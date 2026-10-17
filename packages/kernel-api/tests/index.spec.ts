import { describe, expect, it } from 'vitest'
import { KERNEL_API_VERSION } from '@llmchip/kernel-api'

describe('kernel-api', () => {
  it('exposes a semver API version', () => {
    expect(KERNEL_API_VERSION).toMatch(/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/)
  })
})
