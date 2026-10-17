import { Ajv2020, type ValidateFunction } from 'ajv/dist/2020.js'
import { KERNEL_SCHEMA_ID, kernelSchema } from '@llmchip/kernel-api'
import { KernelError } from './errors.ts'

const ajv = new Ajv2020({ strict: true, allowUnionTypes: true, allErrors: true, useDefaults: true, validateFormats: false })
ajv.addSchema(kernelSchema)

const cache = new Map<string, ValidateFunction>()

/** Validator for a definition of the kernel schema. Applies schema defaults in place. */
export function validatorFor(def: string): ValidateFunction {
  let validate = cache.get(def)
  if (!validate) {
    validate = ajv.getSchema(`${KERNEL_SCHEMA_ID}#/$defs/${def}`)
    if (!validate) throw new Error(`unknown kernel schema definition: ${def}`)
    cache.set(def, validate)
  }
  return validate
}

/** Validates `value` against a kernel schema definition, throwing `code` on failure. */
export function assertValid<T>(def: string, value: unknown, code = 'SCHEMA_INVALID'): asserts value is T {
  const validate = validatorFor(def)
  if (!validate(value)) {
    const errors = (validate.errors ?? []).map((e) => `${e.instancePath || '/'} ${e.message}`)
    throw new KernelError(code, `${def}: ${errors.join('; ')}`, { def, errors })
  }
}
