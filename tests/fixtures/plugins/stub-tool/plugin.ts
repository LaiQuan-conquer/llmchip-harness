// A stand-in for an EDA tool plus its report parser, with handlers that
// misbehave on purpose to exercise the scheduler and the verdict engine.

import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { setTimeout as sleep } from 'node:timers/promises'
import { runPlugin } from '@llmchip/plugin-sdk'

runPlugin({
  id: 'stub-tool',
  version: '1.0.0',
  handlers: {
    report: (ctx) => {
      const { violations, truncate, coverage, crash, limit, conditional } = ctx.params as {
        violations: number
        truncate: boolean
        coverage: boolean
        crash: boolean
        limit?: number
        conditional: boolean
      }
      if (crash) process.exit(2)
      const design = ctx.input('design')
      const lines = [`design ${design.hash}`, `violations ${violations}`, ...(truncate ? [] : ['end of report'])]
      writeFileSync(join(ctx.outDir, 'report.txt'), lines.join('\n'))
      console.log('report written')
      return {
        outputs: [{ name: 'report', path: 'report.txt', type: 'test.report' }],
        // A truncated report cannot be parsed completely, so the parser does not claim metrics it did not see.
        // `limit`: a target the report judges only when one is set (a conditional criterion);
        // `conditional: false` omits the condition metric (a parser that does not report it).
        metrics: truncate
          ? {}
          : {
              violations,
              ...(coverage ? { 'coverage.complete': true } : {}),
              ...(conditional ? { 'limit.set': limit !== undefined } : {}),
              ...(limit !== undefined ? { 'limit.met': violations <= limit } : {}),
            },
        parse: { status: truncate ? 'incomplete' : 'complete', parser: 'stub-report/1' },
      }
    },
    qualify: (ctx) => ({ outputs: [], metrics: { detected: (ctx.params as { detected?: boolean }).detected ?? true }, parse: { status: 'complete', parser: 'stub-qualify/1' } }),
    sleep: async (ctx) => {
      const { ms } = ctx.params as { ms: number }
      for (let waited = 0; waited < ms; waited += 50) {
        if (ctx.signal.aborted) throw new Error('cancelled')
        ctx.progress(waited / ms)
        await sleep(50)
      }
      return { outputs: [], metrics: { slept: ms }, parse: null }
    },
    hang: () => new Promise(() => {}),
    crash: () => process.exit(3),
    fail: () => {
      throw new Error('the tool reported an error')
    },
    escape: () => ({ outputs: [{ name: 'x', path: '../../../escape.txt', type: 'test.report' }] }),
  },
})
