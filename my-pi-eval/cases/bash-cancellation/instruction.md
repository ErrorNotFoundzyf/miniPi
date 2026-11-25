Start the reviewed long-running Python fixture through the native foreground
Bash tool. As soon as the child has created its readiness marker, send a
cancellation signal. The tool must report interruption within three seconds,
and the child must not reach its normal completion marker.
