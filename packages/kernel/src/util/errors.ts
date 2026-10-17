import type { ErrorData } from '@llmchip/kernel-api'

/**
 * An expected, reportable failure. `code` is a stable message code that
 * clients render in the user's language (architecture §9.1); `message` is an
 * English fallback for logs.
 */
export class KernelError extends Error {
  readonly code: string
  readonly params: Record<string, unknown>

  constructor(code: string, message: string, params: Record<string, unknown> = {}) {
    super(message)
    this.name = 'KernelError'
    this.code = code
    this.params = params
  }

  toData(): ErrorData {
    return { code: this.code, params: this.params }
  }
}
