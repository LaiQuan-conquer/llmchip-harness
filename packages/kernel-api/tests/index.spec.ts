import { Ajv2020 } from 'ajv/dist/2020.js'
import { describe, expect, it } from 'vitest'
import {
  KERNEL_API_VERSION,
  KERNEL_EVENT_PAYLOAD_DEFS,
  KERNEL_METHOD_DEFS,
  KERNEL_SCHEMA_ID,
  kernelSchema,
  maxLabel,
} from '@llmchip/kernel-api'

describe('kernel-api', () => {
  it('exposes a semver API version', () => {
    expect(KERNEL_API_VERSION).toMatch(/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/)
  })

  it('compiles as a strict JSON Schema 2020-12 document', () => {
    const ajv = new Ajv2020({ strict: true, allErrors: true, validateFormats: false })
    ajv.addSchema(kernelSchema)
    expect(kernelSchema.$id).toBe(KERNEL_SCHEMA_ID)
    for (const name of Object.keys(kernelSchema.$defs)) {
      expect(ajv.getSchema(`${KERNEL_SCHEMA_ID}#/$defs/${name}`), name).toBeTypeOf('function')
    }
  })

  it('names only existing definitions for methods and event payloads', () => {
    const defs = new Set(Object.keys(kernelSchema.$defs))
    for (const { params, result } of Object.values(KERNEL_METHOD_DEFS)) {
      expect(defs).toContain(params)
      expect(defs).toContain(result)
    }
    for (const def of Object.values(KERNEL_EVENT_PAYLOAD_DEFS)) expect(defs).toContain(def)
  })

  it('orders labels by sensitivity', () => {
    expect(maxLabel([])).toBe('public')
    expect(maxLabel(['internal', 'public'])).toBe('internal')
    expect(maxLabel(['public', 'nda', 'internal'])).toBe('nda')
  })
})
