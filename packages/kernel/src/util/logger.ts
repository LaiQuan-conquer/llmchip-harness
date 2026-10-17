import { appendFileSync } from 'node:fs'

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'

export interface Logger {
  debug(message: string, fields?: Record<string, unknown>): void
  info(message: string, fields?: Record<string, unknown>): void
  warn(message: string, fields?: Record<string, unknown>): void
  error(message: string, fields?: Record<string, unknown>): void
}

/**
 * JSON-lines logger. Every line passes through `redact` before it is written,
 * so secret values never reach the log file or the console (NFR-07).
 */
export function createLogger(options: {
  file?: string
  console?: (line: string) => void
  redact?: (text: string) => string
  level?: LogLevel
}): Logger {
  const order: LogLevel[] = ['debug', 'info', 'warn', 'error']
  const min = order.indexOf(options.level ?? 'info')
  const redact = options.redact ?? ((text: string) => text)
  const write = (level: LogLevel, message: string, fields?: Record<string, unknown>) => {
    if (order.indexOf(level) < min) return
    const line = redact(JSON.stringify({ ts: new Date().toISOString(), level, message, ...fields }))
    if (options.file) appendFileSync(options.file, line + '\n')
    options.console?.(line)
  }
  return {
    debug: (message, fields) => write('debug', message, fields),
    info: (message, fields) => write('info', message, fields),
    warn: (message, fields) => write('warn', message, fields),
    error: (message, fields) => write('error', message, fields),
  }
}
