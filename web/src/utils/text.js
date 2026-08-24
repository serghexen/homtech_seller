export function normalizeEscapedLineBreaks(value) {
  return String(value ?? '').replace(/\\r\\n|\\n|\\r/g, '\n')
}
