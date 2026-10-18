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

const PAGES = ['overview', 'agents', 'trajectory', 'estimator', 'playground', 'approvals', 'incidents', 'artifacts', 'settings']

describe.skipIf(!existsSync(DIST))('Web UI in a browser', () => {
  it('renders every page in both languages without errors or raw keys', { timeout: 120_000 }, async (ctx) => {
    const chromium = await browser()
    if (!chromium) return ctx.skip()
    const { kernel } = await startKernel(undefined, { bundled: true })
    const client = await connect(kernel)
    await client.call('projects.create', { id: 'ui', title: 'UI test' })
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
          expect(text, `${lang}/${name}`).not.toMatch(/\b(nav|common|overview|agents|trajectory|estimator|approvals|incidents|artifacts|settings|status|feasibility|playground)\.[a-z]\w*/)
        }
        const nav = await page.innerText('.top nav')
        expect(nav).toContain(lang === 'zh' ? '项目总览' : 'Overview')
      }
      expect(errors).toEqual([])
    } finally {
      await chromium.close()
    }
  })
})
