// The container executor (architecture §4.4, §9.3) with a small Python image:
// IF-2 over `podman run -i`, no network, read-only inputs, cancellation that
// removes the container. Skipped, visibly, where Podman or the image is missing.

import { execFileSync } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import { connect, FIXTURE_PLUGINS, startKernel } from './helpers.ts'

const IMAGE = 'docker.io/library/python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1'

function available(image: string): boolean {
  try {
    execFileSync('podman', ['image', 'exists', image], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

/** Containers of one run (other tests may run containers concurrently). */
function containers(run: string): string[] {
  return execFileSync('podman', ['ps', '-a', '--filter', `label=llmchip.run=${run}`, '--format', '{{.Names}}'], { encoding: 'utf8' }).split('\n').filter(Boolean)
}

describe.skipIf(!available(IMAGE))('container executor', () => {
  it('runs a plugin in a sandbox without network and with read-only inputs', { timeout: 120_000 }, async () => {
    const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] } } })
    const client = await connect(kernel)
    const data = await client.upload('hello container', { type: 'test.design' })
    const run = await client.call('runs.wait', {
      run: (await client.call('runs.start', { plugin: 'container-stub', handler: 'probe', inputs: [{ name: 'data', hash: data.hash }] })).id,
      timeoutMs: 110_000,
    })
    expect(run).toMatchObject({ execution: 'exited_zero', metrics: { network: false, inputs_writable: false } })
    expect(run.metrics.input_path).toMatch(/^\/llmchip\/in\//)
    expect(await (await client.download(run.outputs[0]!.hash)).text()).toBe('HELLO CONTAINER')
    expect(await client.call('artifacts.verify')).toMatchObject({ ok: true })
    expect(containers(run.id)).toEqual([])
  })

  it('removes the container when a run is cancelled', { timeout: 120_000 }, async () => {
    const { kernel } = await startKernel(undefined, { config: { plugins: { searchPaths: [FIXTURE_PLUGINS] }, scheduler: { cancelGraceMs: 500 } } })
    const client = await connect(kernel)
    const started = await client.call('runs.start', { plugin: 'container-stub', handler: 'sleep', params: { ms: 60_000 } })
    await expect.poll(() => containers(started.id).length, { timeout: 60_000 }).toBe(1)
    await client.call('runs.cancel', { run: started.id })
    expect(await client.call('runs.wait', { run: started.id })).toMatchObject({ execution: 'cancelled' })
    await expect.poll(() => containers(started.id), { timeout: 30_000 }).toEqual([])
  })
})
