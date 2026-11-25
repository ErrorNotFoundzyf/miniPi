"""纯函数 ReAct 微内核 (loop.py) 与上下文清洗 (_provider_context)。

本模块将 ReAct 循环彻底解耦为无状态、可组合的异步生成器，外部宿主只需消费事件流。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from my_agent_core.cancellation import CancellationToken, OperationCancelled
from my_agent_core.events import (
    AgentEnd,
    AgentStart,
    AutoRetryEnd,
    AutoRetryStart,
    ContextCompacted,
    Event,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    TurnEnd,
    TurnStart,
)
from my_agent_core.hooks import (  # pyright: ignore[reportMissingImports]
    BeforeModelCallHook,
    HookResult,
    ToolCallHook,
    ToolResultHook,
)
from my_agent_core.registry import ToolRegistry
from my_agent_core.retry import (  # pyright: ignore[reportMissingImports]
    AutoRetryPolicy,
    extract_retry_after,
)
from my_agent_core.tool_history import (
    _INTERRUPTED_TOOL_RESULT,
    clean_provider_context,
)
from my_agent_core.tools import ToolResult
from my_agent_core.types import ContextManagerProtocol, LLMProtocol
from my_agent_llm import Message, MessageContent, StreamChunk, ToolCall
from my_agent_llm.events import (  # pyright: ignore[reportMissingImports]
    StreamDoneEvent,
    StreamErrorEvent,
    StreamEvent,
    StreamStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDoneEvent,
)
from my_agent_llm.stream import (  # pyright: ignore[reportMissingImports]
    StreamAccumulator,
    close_stream,
)
from my_agent_llm.request_stats import RequestKind, record_model_usage, track_model_request

logger = logging.getLogger(__name__)

__all__ = [
    "CancellationToken",
    "_assistant_turn",
    "_execute_tools_turn",
    "_synthesize_interrupted_tool_calls",
    "run_agent_loop",
]


def _cancelled_assistant(tool_calls: Sequence[ToolCall] = ()) -> MessageEnd:
    metadata: dict[str, Any] = {"stop_reason": "cancelled"}
    if tool_calls:
        metadata["tool_calls"] = [call.model_dump() for call in tool_calls]
    return MessageEnd(Message(role="assistant", content="", metadata=metadata))


async def _counted_model_events(source: AsyncIterator[StreamEvent], kind: RequestKind) -> AsyncIterator[StreamEvent]:
    with track_model_request(kind):
        async with close_stream(source):
            async for event in source:
                if isinstance(event, StreamDoneEvent):
                    record_model_usage(event.usage)
                yield event


async def _assistant_turn(
    *,
    llm: LLMProtocol | Any,
    view: list[Message],
    tool_schemas: list[dict[str, Any]],
    model: str | None = None,
    signal: CancellationToken | None = None,
    context_manager: ContextManagerProtocol | Any | None = None,
    retry_policy: AutoRetryPolicy | None = None,
) -> AsyncIterator[Event]:
    """专职大模型推理车间：纯粹转译模型层产出的高阶 StreamEvent（对标 Tau 并支持智能重试退避）。"""
    policy = retry_policy or AutoRetryPolicy(max_retries=0)
    attempt = 0

    while True:
        if signal is not None and signal.is_cancelled():
            yield _cancelled_assistant()
            return
        attempt += 1
        is_retry = attempt > 1

        event_stream: Any
        # 选择模型事件流
        astream_fn = getattr(llm, "astream_events", None)
        if callable(astream_fn):
            event_stream = astream_fn(messages=view, tools=tool_schemas, model=model, signal=signal)
        else:
            acc = StreamAccumulator()
            event_stream = acc.stream(
                llm.achat_stream(messages=view, tools=tool_schemas, model=model),
                signal=signal,
            )

        events_yielded: list[Event] = []
        error_event: StreamErrorEvent | None = None
        completed: MessageEnd | None = None
        completed_calls: dict[str, ToolCall] = {}

        try:
            event_stream = _counted_model_events(event_stream, "retry" if is_retry else "conversation")
            if signal is not None:
                event_stream = signal.stream(event_stream)
            async with close_stream(event_stream):
                async for ev in event_stream:
                    if isinstance(ev, ToolCallDoneEvent):
                        completed_calls[ev.tool_call.id] = ev.tool_call
                    if signal is not None and signal.is_cancelled():
                        if isinstance(ev, StreamDoneEvent):
                            for call in (ev.message.metadata or {}).get("tool_calls", []):
                                tool_call = _coerce_tool_call(call)
                                completed_calls[tool_call.id] = tool_call
                        continue
                    if isinstance(ev, StreamErrorEvent):
                        error_event = ev
                        break
                    if isinstance(ev, StreamStartEvent):
                        events_yielded.append(MessageStart(ev.partial))
                        continue
                    while events_yielded:
                        yield events_yielded.pop(0)
                    if isinstance(ev, TextDeltaEvent):
                        yield MessageUpdate(message=ev.partial, chunk=StreamChunk(content=ev.delta))
                    elif isinstance(ev, ThinkingDeltaEvent):
                        yield MessageUpdate(
                            message=ev.partial,
                            chunk=StreamChunk(content="", metadata={"reasoning_content": ev.delta}),
                        )
                    elif isinstance(ev, ToolCallDoneEvent):
                        yield MessageUpdate(
                            message=ev.partial,
                            chunk=StreamChunk(content="", tool_calls=[ev.tool_call]),
                        )
                    elif isinstance(ev, StreamDoneEvent):
                        if ev.usage and context_manager is not None:
                            rec_fn = getattr(context_manager, "record_usage", None)
                            if callable(rec_fn):
                                with contextlib.suppress(Exception):
                                    rec_fn(ev.usage)
                        completed = MessageEnd(ev.message)
                        break
        except OperationCancelled:
            yield _cancelled_assistant(list(completed_calls.values()))
            return

        if error_event is not None:
            # 1. 用户主动取消信号触发，直接终止，绝不重试
            if signal is not None and signal.is_cancelled():
                while events_yielded:
                    yield events_yielded.pop(0)
                yield _cancelled_assistant(list(completed_calls.values()))
                return

            exc = error_event.exc
            err_msg = str(exc) if exc is not None else str(error_event.error.content)

            # 2. 判定是否满足重试条件 (未超过最大重试次数且属于可重试瞬时异常)
            if exc is not None and policy.is_retryable(exc) and attempt <= policy.max_retries:
                retry_after = extract_retry_after(exc)
                try:
                    delay_ms = int(policy.compute_delay_ms(attempt=attempt, retry_after=retry_after))
                except Exception:
                    delay_ms = 1000

                yield AutoRetryStart(
                    attempt=attempt,
                    max_attempts=policy.max_retries,
                    delay_ms=delay_ms,
                    error_message=err_msg,
                )

                # 可中断的延迟等待 (50ms 颗粒度轮询取消信号)
                sleep_sec = delay_ms / 1000.0
                cancelled_during_wait = False
                if signal is not None:
                    step = 0.05
                    slept = 0.0
                    while slept < sleep_sec:
                        if signal.is_cancelled():
                            cancelled_during_wait = True
                            break
                        wait_slice = min(step, sleep_sec - slept)
                        await asyncio.sleep(wait_slice)
                        slept += wait_slice
                else:
                    await asyncio.sleep(sleep_sec)

                if cancelled_during_wait:
                    yield AutoRetryEnd(success=False, attempt=attempt, final_error="Cancelled by user")
                    yield _cancelled_assistant()
                    return

                # 继续下一轮重试循环
                continue
            else:
                # 致命不可重试错误或重试次数耗尽
                if is_retry:
                    yield AutoRetryEnd(success=False, attempt=attempt - 1, final_error=err_msg)
                while events_yielded:
                    yield events_yielded.pop(0)
                yield MessageEnd(error_event.error)
                return
        else:
            # 成功完成推理！如果此前经历过重试，发射 AutoRetryEnd(success=True)
            if completed is not None:
                yield completed
            if is_retry:
                yield AutoRetryEnd(success=True, attempt=attempt - 1)
            return


def _as_messages(items: Sequence[Message | MessageContent]) -> list[Message]:
    """安全归一化字符串或消息序列为标准 Message 列表。"""
    return [m if isinstance(m, Message) else Message(role="user", content=m) for m in items]


def _synthesize_interrupted_tool_calls(
    tool_calls: Sequence[Any],
) -> list[Message]:
    """统一生成标准的中断工具结果，彻底消除多处代码重复。"""
    res = []
    for tc in tool_calls:
        c = _coerce_tool_call(tc)
        res.append(
            Message(
                role="tool",
                content=_INTERRUPTED_TOOL_RESULT,
                metadata={"tool_call_id": c.id, "tool_name": c.name, "is_error": True},
            )
        )
    return res


def _coerce_tool_call(tc: Any) -> ToolCall:
    """安全归一化工具调用为 ToolCall 实体，Never-Throw 捕获畸形入参。"""
    if isinstance(tc, ToolCall):
        return tc
    try:
        if isinstance(tc, dict) and "id" not in tc:
            tc = {**tc, "id": ""}
        return ToolCall.model_validate(tc)
    except Exception as exc:
        raw_id = getattr(tc, "id", None)
        if not raw_id:
            raw_id = tc.get("id", "") if isinstance(tc, dict) else ""
        raw_name = getattr(tc, "name", None)
        if not raw_name:
            raw_name = tc.get("name", "") if isinstance(tc, dict) else ""
        return ToolCall(
            id=str(raw_id if raw_id else ""),
            name=str(raw_name if raw_name else ""),
            error=f"Invalid tool call: {exc}",
        )


async def _fail_tool_calls_from_truncated_message(
    tool_calls: Sequence[Any],
) -> AsyncIterator[Event]:
    """阶段 1: 当模型因触达 Token 上限导致输出截断 (stop_reason='length') 时，安全拦截所有工具调用。

    防范流式 salvage 拼装出残缺的 JSON 参数导致文件写崩或命令截断。
    生成清晰的重试提示结果回传给大模型，引导其重新完整发起调用。
    """
    for tc in tool_calls:
        call = _coerce_tool_call(tc)
        yield ToolExecutionStart(
            tool_call_id=call.id,
            tool_name=call.name,
            args=call.args,
        )
        err_msg = (
            f'Tool call "{call.name}" was not executed: the response hit the output '
            "token limit, so its arguments may be truncated. Re-issue the tool call with complete arguments."
        )
        yield ToolExecutionEnd(
            tool_call_id=call.id,
            tool_name=call.name,
            result=err_msg,
            is_error=True,
            execution_status="skipped",
        )
        tool_msg = Message(
            role="tool",
            content=err_msg,
            metadata={"tool_call_id": call.id, "tool_name": call.name, "is_error": True},
        )
        yield MessageStart(tool_msg)
        yield MessageEnd(tool_msg)

# 一批工具到底是怎样执行的？
async def _execute_tools_turn(
    *,
    tool_calls: Sequence[Any],
    registry: ToolRegistry,
    before_tool_call: (Callable[[ToolCallHook], Awaitable[HookResult | None] | HookResult | None] | None) = None,
    after_tool_call: (Callable[[ToolResultHook], Awaitable[HookResult | None] | HookResult | None] | None) = None,
    signal: CancellationToken | None = None,
) -> AsyncIterator[Event]:
    """专职工具批处理执行车间：Preflight 广播 -> 审批改参 -> 并发批执行 -> 结果改写 -> 结果广播。

    严格遵循 Pi 时序契约：
    1. Preflight 阶段：在调用 before_tool_call 审查与执行之前，率先按 source order 广播 ToolExecutionStart；
    2. 审查阶段：调用 before_tool_call 审批与入参改写；
    3. Execution 阶段：并发批量执行未阻断工具，支持实时流式进度回传 (ToolExecutionUpdate) 与中途取消自愈；
    4. Completion 阶段：按 source order 执行 after_tool_call 改写并广播 ToolExecutionEnd (含 terminate 状态)；
    5. Message 阶段：按 source order 发射 role="tool" 的 MessageStart / MessageEnd。
    """
    # ── 阶段 A1: Preflight 广播（按 source order 先行发射 ToolExecutionStart）
    # 【学习注释】统一不同提供方的工具调用格式，先通知界面，尚未执行工具。
    parsed_calls: list[ToolCall] = [_coerce_tool_call(tc) for tc in tool_calls]
    # 先通知外层，模型提出了这些工具请求
    for call in parsed_calls:
        yield ToolExecutionStart(tool_call_id=call.id, tool_name=call.name, args=call.args)

    # ── 阶段 A2: 前置审查与参数改写 (before_tool_call)
    # 【学习注释】记录通过审查的调用和已经被阻止/出错的结果，索引用于恢复顺序。
    prepared_calls: list[tuple[int, ToolCall, dict[str, Any]]] = []
    direct_results: dict[int, ToolResult] = {}
    blocked_calls: set[int] = set()
    dispatched_args: dict[int, dict[str, Any]] = {}

    for idx, call in enumerate(parsed_calls):
        if call.error is not None:
            direct_results[idx] = ToolResult(ok=False, error=call.error)
            continue

        if signal is not None and signal.is_cancelled():
            direct_results[idx] = ToolResult(ok=False, error=_INTERRUPTED_TOOL_RESULT)
            continue

        current_args = call.args
        err: str | None = None
        block_terminate: bool = False
        if before_tool_call is not None:
            try:
                # 在关键时刻插入的一段自定义检查逻辑。
                # 【学习注释】Hook 是可插入的回调：可观察、阻止调用，或返回替换参数。
                decision = before_tool_call(ToolCallHook(tool_call_id=call.id, tool_name=call.name, args=current_args))
                if inspect.isawaitable(decision):
                    decision = await decision
                if decision is not None:
                    if decision.block:
                        blocked_calls.add(idx)
                        err = f"Tool '{call.name}' blocked: {decision.reason or 'blocked by policy'}"
                        if decision.terminate is not None:
                            block_terminate = decision.terminate
                    elif decision.updated_args is not None:
                        current_args = decision.updated_args
            except Exception as exc:
                err = f"Error in before_tool_call for '{call.name}': {exc}"

        if err is not None:
            direct_results[idx] = ToolResult(ok=False, error=err, terminate=block_terminate)
        else:
            prepared_calls.append((idx, call, current_args))

    # ── 阶段 B: 并发批执行与实时进度流式广播 (Phase 5)
    if signal is not None and signal.is_cancelled():
        for idx, _, _ in prepared_calls:
            direct_results[idx] = ToolResult(ok=False, error=_INTERRUPTED_TOOL_RESULT)
    elif prepared_calls:
        # 建立线程安全事件队列与哨兵
        # 【学习注释】队列连接后台工具任务和事件消费者；哨兵表示整批执行结束。
        queue: asyncio.Queue[Event | object] = asyncio.Queue()
        _SENTINEL = object()
        loop = asyncio.get_running_loop()
        loop_thread_id = threading.get_ident()

        def safe_put(item: Event | object) -> None:
            if threading.get_ident() == loop_thread_id:# 放入一个事件
                queue.put_nowait(item)
            else:
                with contextlib.suppress(RuntimeError):
                    # 【学习注释】工作线程不能直接操作事件循环的队列，交由循环线程投递。
                    loop.call_soon_threadsafe(queue.put_nowait, item)

        def make_on_update(call_id: str, tool_name: str, args: dict[str, Any]) -> Callable[[Any], None]:
            def on_update(partial: Any) -> None:
                safe_put(
                    ToolExecutionUpdate(
                        tool_call_id=call_id,
                        tool_name=tool_name,
                        args=args,
                        partial_result=partial,
                    )
                )

            return on_update

        calls_to_run = [
            (
                call.name,
                current_args,
                make_on_update(call.id, call.name, current_args),
                call.id,
            )
            for _, call, current_args in prepared_calls
        ]
        dispatched_args = {idx: args.copy() for idx, _, args in prepared_calls}

        async def _run_batch() -> list[ToolResult]:
            try: #前置审查结束后，未被阻止的调用交给execute_batch()
                return await registry.execute_batch(calls_to_run, signal=signal)
            finally:
                safe_put(_SENTINEL)

        # 【学习注释】批处理与取进度事件并发推进；await queue.get() 等待新事件时不占住循环。
        runner = asyncio.create_task(_run_batch())
        try:
            while True:
                item = await queue.get() # 从队列中取出一个事件
                if item is _SENTINEL:
                    break
                if isinstance(item, Event):
                    yield item
            batch_out = await runner
            for (idx, _, _), res in zip(prepared_calls, batch_out, strict=False):
                direct_results[idx] = res
        except Exception as exc:
            for idx, _, _ in prepared_calls:
                if idx not in direct_results:
                    direct_results[idx] = ToolResult(ok=False, error=f"Tool execution failed: {exc}")
        finally:
            if not runner.done():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner

    # 【学习注释】顺序遍历原始调用，保证结果消息排列与模型发出的调用顺序一致。
    # ── 阶段 C: 后置改写与 ToolExecutionEnd 广播
    for idx, call in enumerate(parsed_calls):
        res = direct_results.get(idx, ToolResult(ok=False, error=_INTERRUPTED_TOOL_RESULT))
        if idx in blocked_calls:
            execution_status = "blocked"
        elif idx not in dispatched_args:
            execution_status = "skipped"
        else:
            execution_status = "succeeded" if res.ok else "failed"
        obs = res.serialize()
        is_err = not res.ok
        effective_terminate = res.terminate

        # 文本视图（供 Hook 审计、ToolExecutionEnd 广播及 TUI 显示）
        if isinstance(obs, list):
            text_view = (
                "\n".join(
                    str(item.get("text", "")) for item in obs if isinstance(item, dict) and item.get("type") == "text"
                )
                or "[Multimodal content]"
            )
        else:
            text_view = str(obs)

        # 触发决策点 5: tool_result (after_tool_call 结果篡改与熔断介入)
        if after_tool_call is not None and not (signal is not None and signal.is_cancelled()):
            try:
                decision = after_tool_call(
                    ToolResultHook(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        result=text_view,
                        is_error=is_err,
                        terminate=effective_terminate,
                    )
                )
                if inspect.isawaitable(decision):
                    decision = await decision
                if decision is not None:
                    if decision.block:
                        obs = f"Tool '{call.name}' blocked: {decision.reason or 'blocked by policy'}"
                        text_view = obs
                        is_err = True
                    elif decision.updated_result is not None:
                        obs = decision.updated_result
                        text_view = str(decision.updated_result)
                        is_err = False
                    if decision.terminate is not None:
                        effective_terminate = decision.terminate
            except Exception as exc:
                obs = f"Error in after_tool_call for '{call.name}': {exc}"
                text_view = obs
                is_err = True

        yield ToolExecutionEnd(
            tool_call_id=call.id,
            tool_name=call.name,
            result=text_view,
            is_error=is_err,
            terminate=effective_terminate,
            execution_status=execution_status,
            dispatched_args=dispatched_args.get(idx),
            result_meta=res.meta.copy(),
        )

        # 阶段 7: 产出配对的 Tool 消息实体并携带关键元数据
        tool_msg = Message(
            role="tool",
            content=obs,
            metadata={
                "tool_call_id": call.id,
                "tool_name": call.name,
                "is_error": is_err,
                "terminate": effective_terminate,
            },
        )

        # 发射成对的 MessageStart / MessageEnd 事件驱动多层持久化：
        # 1. 内存层：外层 run_agent_loop 监听到 MessageEnd 会将 tool_msg 追加到 messages 列表，供给下一轮大模型推理；
        # 2. 磁盘层：外壳层 Agent.prompt_stream 监听到 MessageEnd 会触发 session.add_message 原子落盘到 JSONL 文件；
        # 3. 熔断层：通过 metadata["terminate"] 向外透传阶段 7 熔断标记，驱动 ReAct 循环终止。
        yield MessageStart(tool_msg)
        yield MessageEnd(tool_msg)


async def run_agent_loop(
    *,
    llm: LLMProtocol | Any,
    messages: list[Message],
    tools: ToolRegistry | Sequence[Any] | None = None,
    context_manager: ContextManagerProtocol | Any | None = None,
    model: str | None = None,
    system: str = "",
    prompts: Sequence[Message | MessageContent] = (),
    signal: CancellationToken | None = None,
    get_steering_messages: Callable[[], Sequence[Message | MessageContent]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[Message | MessageContent]] | None = None,
    before_model_call: (
        Callable[[BeforeModelCallHook], Awaitable[HookResult | None] | HookResult | None] | None
    ) = None,
    before_tool_call: (Callable[[ToolCallHook], Awaitable[HookResult | None] | HookResult | None] | None) = None,
    after_tool_call: (Callable[[ToolResultHook], Awaitable[HookResult | None] | HookResult | None] | None) = None,
    retry_policy: AutoRetryPolicy | None = None,
) -> AsyncIterator[Event]:
    """对标 Tau 的极简纯函数异步微内核，主状态机约 110 行。"""

    # 准备工具注册表
    if isinstance(tools, ToolRegistry):
        registry = tools
    else:
        registry = ToolRegistry()
        if isinstance(tools, Sequence):
            for t in tools:
                registry.register(t)

    # 初始化协作取消检查
    if signal is not None and signal.is_cancelled():
        yield AgentEnd(
            messages=list(messages),
            final_text=None,
            iterations=0,
            stop_reason="cancelled",
        )
        return

    # system prompt 初始化，处理系统提示词和初始用户消息
    if system and (not messages or messages[0].role != "system"):
        messages.insert(0, Message(role="system", content=system))
    elif not system and messages and messages[0].role == "system":
        system = messages[0].text_content

    # prompts 规范化
    converted_prompts = _as_messages(prompts)

    user_input = converted_prompts[0].text_content if converted_prompts else ""

    # 1. 注入初始 prompts 并发射事件
    yield AgentStart(system_prompt=system, user_input=user_input)
    for p_msg in converted_prompts:
        messages.append(p_msg)
        yield MessageStart(p_msg)
        yield MessageEnd(p_msg)

    iteration = 0
    final_text: str | None = None
    pending_messages: list[Message] = []
    length_continuations = 0
    MAX_LENGTH_CONTINUATIONS = 3
    # 若初始已传入 prompts 任务，首轮推理严格执行初始任务；steering 消息统一在首轮工具执行完毕后交付（对标 Pi 契约）
    if not converted_prompts and get_steering_messages is not None:
        init_steer = get_steering_messages()
        if init_steer:
            pending_messages.extend(_as_messages(init_steer))

    # ══════════════════════════════════════════════════════════
    # 【外层循环】：Follow-up 宏观任务接力
    # ══════════════════════════════════════════════════════════
    # 【学习注释】外层：一项任务结束后，检查是否有 follow-up；有则继续接下一项。
    while True:
        has_more_tools = True

        # ──────────────────────────────────────────────────────
        # 【内层循环】：微观 ReAct 迭代与 Steer 即时转向
        # ──────────────────────────────────────────────────────
        # 【学习注释】内层：一轮模型回答可能要求工具；把结果送回模型，直到无需继续。
        while has_more_tools or pending_messages:
            iteration += 1

            # 轮次开端：先注入待处理的消息并清空 pending_messages (Steering)
            # 这段既可能处理steering，也可能处理上一层取出的follow-up
            if pending_messages:
                for p_msg in pending_messages:
                    messages.append(p_msg)
                    yield MessageStart(p_msg)
                    yield MessageEnd(p_msg)
                pending_messages = []

            yield TurnStart(iteration)

            # 前置清洗与准备本轮上下文
            try:
                if context_manager:
                    preparation = context_manager.prepare(messages)
                    view = await signal.wait(preparation) if signal is not None else await preparation
                else:
                    view = messages
            except OperationCancelled:
                yield TurnEnd(message=None, tool_results=[])
                yield AgentEnd(messages=list(messages), final_text=None, iterations=iteration, stop_reason="cancelled")
                return
            view = clean_provider_context(view) # view是本轮实际准备交给模型的消息集合

            # 派发上下文压缩事件（若触发了 compact 压缩）
            comp_info = getattr(context_manager, "pending_compaction", None) if context_manager is not None else None
            if comp_info is not None:
                yield ContextCompacted(
                    tokens_before=comp_info.tokens_before,
                    tokens_after=comp_info.tokens_after,
                    summarized_count=comp_info.summarized_count,
                )

            # Hook 3: BeforeModelCallHook (context 审查)
            if before_model_call is not None:
                try:
                    decision = before_model_call(BeforeModelCallHook(messages=list(view), iteration=iteration))
                    if inspect.isawaitable(decision):
                        decision = await signal.wait(decision) if signal is not None else await decision
                    if decision is not None:
                        if decision.block:
                            reason = f": {decision.reason}" if decision.reason else ""
                            yield TurnEnd(message=None, tool_results=[])
                            yield AgentEnd(
                                messages=list(messages),
                                final_text=f"(blocked{reason})",
                                iterations=iteration,
                                stop_reason="blocked",
                            )
                            return
                        if decision.updated_messages is not None:
                            view = decision.updated_messages
                except OperationCancelled:
                    pass
                except Exception as exc:
                    logger.warning("Error in before_model_call callback: %s", exc)

            if signal is not None and signal.is_cancelled():
                yield TurnEnd(message=None, tool_results=[])
                yield AgentEnd(messages=list(messages), final_text=None, iterations=iteration, stop_reason="cancelled")
                return

            check_budget = getattr(context_manager, "request_budget_error", None)
            budget_error = check_budget(view) if callable(check_budget) else None
            if budget_error:
                yield TurnEnd(message=None, tool_results=[])
                yield AgentEnd(
                    messages=list(messages),
                    final_text=budget_error,
                    iterations=iteration,
                    stop_reason="error",
                )
                return

            # 委托模型车间
            assistant: Message | None = None
            # 【学习注释】调用模型并逐个转发流式事件；MessageEnd 里是完整 assistant 消息。
            # _assistant_turn()：模型事件如何变成agent事件
            assistant_stream = _assistant_turn(
                llm=llm,
                view=view,
                tool_schemas=registry.get_schemas(),
                model=model,
                signal=signal,
                context_manager=context_manager,
                retry_policy=retry_policy,
            )
            async with contextlib.aclosing(assistant_stream):
                async for ev in assistant_stream:
                    if isinstance(ev, MessageEnd):
                        assistant = ev.message
                        messages.append(assistant)
                    yield ev

            if assistant is None:
                assistant = Message(
                    role="assistant",
                    content="Provider produced no assistant message",
                    metadata={"stop_reason": "error"},
                )
                yield MessageStart(assistant)
                messages.append(assistant)
                yield MessageEnd(assistant)

            # 取消响应、异常阻断与断头自愈
            if assistant.metadata and assistant.metadata.get("stop_reason") in (
                "cancelled",
                "error",
            ):
                stop_reason = assistant.metadata.get("stop_reason", "cancelled")
                calls = assistant.metadata.get("tool_calls", [])
                synth_tools = _synthesize_interrupted_tool_calls(calls)
                for s in synth_tools:
                    messages.append(s)
                    yield MessageStart(s)
                    yield MessageEnd(s)
                yield TurnEnd(message=assistant, tool_results=synth_tools)
                yield AgentEnd(
                    messages=list(messages),
                    final_text=assistant.text_content if stop_reason == "error" else None,
                    iterations=iteration,
                    stop_reason=stop_reason,
                )
                return

            # 委托工具车间
            tool_results: list[Message] = []
            # 【学习注释】检查模型是否提出工具请求；length 意味着输出可能不完整。
            calls = (assistant.metadata or {}).get("tool_calls")
            is_truncated = (assistant.metadata or {}).get("stop_reason") == "length"
            if calls:
                length_continuations = 0
                tool_stream = (
                    # 输出截断防御检查
                    # 当检测到模型输出触达 Token 上限被截断（stop_reason == "length"）时，断然拒绝执行任何工具
                    _fail_tool_calls_from_truncated_message(calls)
                    if is_truncated
                    else _execute_tools_turn( # 发起工具调用
                        tool_calls=calls,
                        registry=registry,
                        before_tool_call=before_tool_call,
                        after_tool_call=after_tool_call,
                        signal=signal,
                    )
                )
                # 【学习注释】转发工具事件；工具消息结束时写入历史，供下一轮模型读取。
                async with contextlib.aclosing(tool_stream):
                    async for ev in tool_stream:
                        if isinstance(ev, MessageEnd) and ev.message.role == "tool":
                            tool_results.append(ev.message)
                            messages.append(ev.message)
                        yield ev

                # 阶段 7: 批量优雅熔断判定（any 语义）
                # 【学习注释】整批完成后检查终止标记；这不会提前取消同批其他工具。
                terminating_obs = [m.text_content for m in tool_results if (m.metadata or {}).get("terminate")]
                if terminating_obs:
                    has_more_tools = False
                    final_text = assistant.text_content or terminating_obs[-1]
                else:
                    has_more_tools = True
            elif is_truncated and length_continuations < MAX_LENGTH_CONTINUATIONS:
                # ── 对标 Pi isRecoverableLength / willRetry ────────────────────────
                # 当大模型因达到 max_tokens 输出截断且尚未吐出 tool_calls 时，
                # 绝不草率退出！注入自动续写提示，引导模型继续输出并调用工具。
                length_continuations += 1
                continuation_msg = Message(
                    role="user",
                    content="[System: Your previous response reached the maximum output token limit. Please continue your response and proceed to call tools to execute your plan.]",
                )
                messages.append(continuation_msg)
                yield MessageStart(continuation_msg)
                yield MessageEnd(continuation_msg)
                has_more_tools = True
            else:
                has_more_tools = False
                final_text = assistant.text_content

            # 严密闭合当前轮次，发出本轮结束事件
            yield TurnEnd(message=assistant, tool_results=tool_results)

            # 先检查signal，取消则产生AgentEnd并返回
            if signal is not None and signal.is_cancelled():
                yield AgentEnd(
                    messages=list(messages),
                    final_text=final_text,
                    iterations=iteration,
                    stop_reason="cancelled",
                )
                return

            # steering检查：之后才读取steering
            if get_steering_messages is not None:
                steer_msgs = get_steering_messages()
                if steer_msgs:
                    pending_messages = _as_messages(steer_msgs)

        # 收割宏观追问任务
        # 【学习注释】followup检查：内层结束后再检查后续任务；有新消息就回到外层，否则结束。
        if get_follow_up_messages is not None:
            followups = get_follow_up_messages()
            if followups:
                pending_messages = _as_messages(followups)
                continue
        break

    yield AgentEnd(
        messages=list(messages),
        final_text=final_text,
        iterations=iteration,
        stop_reason="end_turn",
    )
