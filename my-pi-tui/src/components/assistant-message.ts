import { Container, Markdown, Spacer, Text } from "@earendil-works/pi-tui";
import { getMarkdownTheme, theme } from "../theme/theme.js";

export class AssistantMessageComponent extends Container {
  private thinkingContainer: Container;
  private contentContainer: Container;
  private thinkingText = "";
  private contentText = "";
  constructor(private isThinkingExpanded = false) {
    super();

    this.thinkingContainer = new Container();
    this.contentContainer = new Container();

    this.addChild(this.thinkingContainer);
    this.addChild(this.contentContainer);
  }

  public appendReasoningDelta(delta: string): void {
    this.thinkingText += delta;
    this.updateThinkingDisplay();
  }

  public setReasoning(fullThinking: string): void {
    if (this.thinkingText !== fullThinking) {
      this.thinkingText = fullThinking;
      this.updateThinkingDisplay();
    }
  }

  public appendTextDelta(delta: unknown): void {
    const str = typeof delta === "string" ? delta : String(delta ?? "");
    this.contentText += str;
    this.updateContentDisplay();
  }

  public setContent(fullContent: unknown): void {
    let str = "";
    if (typeof fullContent === "string") {
      str = fullContent;
    } else if (Array.isArray(fullContent)) {
      str = fullContent
        .map((b: any) =>
          typeof b === "string" ? b : b?.text || b?.thinking || "",
        )
        .join("");
    } else if (fullContent !== null && fullContent !== undefined) {
      str = String(fullContent);
    }

    if (this.contentText !== str) {
      this.contentText = str;
      this.updateContentDisplay();
    }
  }

  public getContentText(): string {
    return this.contentText;
  }

  public setThinkingExpanded(expanded: boolean): void {
    if (this.isThinkingExpanded === expanded) return;
    this.isThinkingExpanded = expanded;
    this.updateThinkingDisplay();
  }

  public finalize(): void {
    this.updateContentDisplay();
  }

  private updateThinkingDisplay(): void {
    this.thinkingContainer.clear();
    if (!this.thinkingText.trim()) {
      return;
    }

    if (this.isThinkingExpanded) {
      const label = new Text(
        theme.bold(
          theme.fg("thinkingText", "\u25c8 思考过程 (按 Ctrl+O 折叠):"),
        ),
        1,
        0,
      );
      const md = new Markdown(
        this.thinkingText.trim(),
        1,
        0,
        getMarkdownTheme(),
        {
          color: (t: string) => theme.fg("thinkingText", t),
          italic: true,
        },
      );
      this.thinkingContainer.addChild(label);
      this.thinkingContainer.addChild(md);
      this.thinkingContainer.addChild(new Spacer(1));
    } else {
      const summary = new Text(
        theme.italic(
          theme.fg(
            "thinkingText",
            `\u25c8 思考过程 (${this.thinkingText.trim().length} 字符，按 Ctrl+O 展开)`,
          ),
        ),
        1,
        0,
      );
      this.thinkingContainer.addChild(summary);
      this.thinkingContainer.addChild(new Spacer(1));
    }
  }

  private updateContentDisplay(): void {
    this.contentContainer.clear();
    if (!this.contentText.trim()) {
      return;
    }

    const md = new Markdown(this.contentText.trim(), 1, 0, getMarkdownTheme());
    this.contentContainer.addChild(md);
  }
}
