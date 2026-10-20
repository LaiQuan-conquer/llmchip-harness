// The Web UI (M1d): both language catalogs are complete (AC-M1-12), every
// text key used in the code exists, and the UI served by harnessd renders
// every page in both languages without errors or untranslated keys, in a
// headless browser where one is installed.

import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { CATALOGS, CODES } from '../apps/web/src/i18n.ts'
import { readRuntimeInfo } from '@llmchip/kernel'
import { connect, startKernel } from './helpers.ts'

const SRC = resolve(import.meta.dirname, '../apps/web/src')
const DIST = resolve(import.meta.dirname, '../apps/web/dist/index.html')
const TINY_QWEN3 = resolve(import.meta.dirname, 'fixtures/models/tiny-qwen3')

function sources(dir: string): string[] {
  return readdirSync(dir, { recursive: true, encoding: 'utf8' })
    .filter((f) => f.endsWith('.tsx') || f.endsWith('.ts'))
    .map((f) => readFileSync(join(dir, f), 'utf8'))
}

describe('Web UI texts (AC-M1-12)', () => {
  it('has the same keys and placeholders in Chinese and English', () => {
    expect(Object.keys(CATALOGS.en).sort()).toEqual(Object.keys(CATALOGS.zh).sort())
    expect(Object.keys(CODES.en).sort()).toEqual(Object.keys(CODES.zh).sort())
    const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
    for (const key of Object.keys(CATALOGS.zh) as (keyof typeof CATALOGS.zh)[]) {
      expect(placeholders(CATALOGS.en[key]), key).toEqual(placeholders(CATALOGS.zh[key]))
      expect(CATALOGS.en[key].trim(), key).not.toBe('')
    }
  })

  it('uses only keys that exist', () => {
    const used = new Set<string>()
    for (const text of sources(SRC)) for (const m of text.matchAll(/\bt\('([\w.]+)'/g)) used.add(m[1]!)
    expect(used.size).toBeGreaterThan(50)
    for (const key of used) expect(Object.keys(CATALOGS.zh), key).toContain(key)
  })
})

async function browser() {
  try {
    const { chromium } = await import('playwright-core')
    return await chromium.launch()
  } catch {
    return undefined
  }
}

/** Every catalog key; none may appear verbatim in a rendered page. */
const CATALOG_KEYS = Object.keys(CATALOGS.zh)

const PAGES = ['flow', 'overview', 'agents', 'trajectory', 'estimator', 'playground', 'approvals', 'incidents', 'artifacts', 'settings']

describe.skipIf(!existsSync(DIST))('Web UI in a browser', () => {
  it('renders every page in both languages without errors or raw keys', { timeout: 240_000 }, async (ctx) => {
    const chromium = await browser()
    if (!chromium) return ctx.skip()
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'ui', title: 'UI test' })
    // An estimate in the project, so the S0 detail view has a real report to show.
    const data = (await client.call('plugins.list')).plugins.flatMap((p) => p.data)
    const profile = data.find((d) => d.id === 'gf180mcu')!.hash
    const library = data.find((d) => d.type === 'implementation.library')!.hash
    const estimate = await client.call('runs.start', {
      plugin: 'estimator',
      handler: 'estimate',
      project: 'ui',
      params: { model: { preset: 'stories260K' }, weightBits: 2, weightStorage: 'gf180mcu.stdcell-rom', targetAreaMm2: 2.4 },
      inputs: [
        { name: 'profile', hash: profile },
        { name: 'library', hash: library },
      ],
    })
    expect(await client.call('runs.wait', { run: estimate.id, timeoutMs: 60_000 })).toMatchObject({ execution: 'exited_zero' })
    // A real (tiny) model imported and recovered, for the census and the S1 graph view.
    const pipeline = await import('../apps/web/src/views/modelPipeline.ts')
    await pipeline.startFromWeights(client, { path: TINY_QWEN3, project: 'ui', title: 'tiny-qwen3', bits: 4, process: 'gf180mcu' }, () => {})
    await pipeline.recoverGraph(client, 'ui', () => {})
    // A numeric scheme of the recovered graph, for the S2 view (L2 against L0).
    await pipeline.runScheme(client, 'ui', 'graph', { weightFormat: 'int8', groupSize: 16, rounding: 'half-even', overflow: 'saturate' }, { maxPrompts: 1, steps: 2, passageTokens: 64 }, () => {})
    // The S3 analysis of the recovered graph (design space of the engine at the model's dimensions).
    await pipeline.exploreArch(client, 'ui', { parallelMacs: 2, clockMHz: 8, memoryInterface: 'sqi', operandBuffers: 'none' }, () => {})
    const runtime = readRuntimeInfo(kernel.layout.root)!
    try {
      const page = await chromium.newPage()
      const errors: string[] = []
      page.on('pageerror', (e) => errors.push(String(e)))
      page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))

      // Without the token the UI explains how to open it.
      await page.goto(runtime.url)
      await page.waitForSelector('.splash')

      await page.goto(`${runtime.url}/#token=${runtime.token}`)
      await page.waitForSelector('.app')
      for (const lang of ['zh', 'en'] as const) {
        await page.click('button[data-page=settings]')
        await page.click(`button[data-lang=${lang}]`)
        for (const name of PAGES) {
          await page.click(`button[data-page=${name}]`)
          await page.waitForTimeout(150)
          const text = await page.innerText('.app')
          // No catalog key leaked into the page, e.g. "nav.overview".
          expect(CATALOG_KEYS.filter((key) => text.includes(key)), `${lang}/${name}`).toEqual([])
          expect(text, `${lang}/${name}`).not.toMatch(/\b(nav|common|overview|agents|trajectory|estimator|approvals|incidents|artifacts|settings|status|feasibility|playground)\.[a-z]\w*/)
        }
        // The S0 detail view, opened from its card on the flow page.
        await page.click('button[data-page=flow]')
        await page.click('#flow-S0 .flow-detail')
        await page.waitForSelector('.s0-answer')
        const detail = await page.innerText('.app')
        expect(CATALOG_KEYS.filter((key) => detail.includes(key)), `${lang}/S0`).toEqual([])
        expect(detail).toContain(lang === 'zh' ? '张量普查' : 'Tensor census')
        await page.click('button.step-back')
        // The S1 view: the recovered graph, its tensor map, L0 and the comparison.
        await page.click('#flow-S1 .flow-detail')
        await page.waitForSelector('.s1-graph')
        const s1 = await page.innerText('.app')
        expect(CATALOG_KEYS.filter((key) => s1.includes(key)), `${lang}/S1`).toEqual([])
        expect(s1).toContain(lang === 'zh' ? 'Q 逐头 RMSNorm' : 'Per-head RMSNorm of Q')
        expect(s1).toContain('transformers')
        expect(s1).toContain(lang === 'zh' ? '全量测试结果' : 'Full test results')
        // A test item opens its details; a deliverable opens the readable artifact view.
        await page.click('.s1-tests tbody tr:nth-child(4)')
        await page.click('button.step-back')
        await page.click('#flow-S1 .flow-outputs button')
        await page.waitForSelector('.flow-what')
        const artifact = await page.innerText('.app')
        expect(CATALOG_KEYS.filter((key) => artifact.includes(key)), `${lang}/artifact`).toEqual([])
        // The back bar returns from the artifact to the flow page it was opened from.
        expect(await page.innerText('.nav-back')).toContain(lang === 'zh' ? '演示流程' : 'Flow')
        await page.click('.nav-back')
        await page.waitForSelector('#flow-S1 .flow-detail')
        // The selected project in the sidebar opens the stages' detailed demos.
        await page.click('.side-entries li:nth-child(3)')
        await page.waitForSelector('.s1-graph')
        await page.click('.nav-back')
        await page.waitForSelector('#flow-S1 .flow-detail')
        // The S2 view: the scheme explained, the bench, the comparison and the accuracy against L0.
        await page.click('#flow-S2 .flow-detail')
        await page.waitForSelector('.s2-compare')
        await page.waitForSelector('.s2-bars')
        const s2 = await page.innerText('.app')
        expect(CATALOG_KEYS.filter((key) => s2.includes(key)), `${lang}/S2`).toEqual([])
        expect(s2).not.toMatch(/\bs2\.[a-z]/)
        expect(s2).toContain(lang === 'zh' ? '偶数舍入' : 'round half to even')
        expect(s2).toContain(lang === 'zh' ? '累加器' : 'accumulator')
        await page.click('.nav-back')
        await page.waitForSelector('#flow-S2 .flow-detail')
        // The S3 view: the analysis, the design space and its table.
        await page.click('#flow-S3 .flow-detail')
        await page.waitForSelector('.s3-scatter')
        const s3 = await page.innerText('.app')
        expect(CATALOG_KEYS.filter((key) => s3.includes(key)), `${lang}/S3`).toEqual([])
        expect(s3).not.toMatch(/\bs3\.[a-z]/)
        expect(s3).toContain(lang === 'zh' ? '设计空间' : 'Design space')
        await page.click('.nav-back')
        await page.waitForSelector('#flow-S3 .flow-detail')
        await page.click('button[data-page=flow]')
        const nav = await page.innerText('.top nav')
        expect(nav).toContain(lang === 'zh' ? '项目总览' : 'Overview')
      }
      expect(errors).toEqual([])
    } finally {
      await chromium.close()
    }
  })
})
