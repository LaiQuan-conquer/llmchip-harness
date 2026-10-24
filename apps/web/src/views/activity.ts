// Plain-language activity feed: the events a person follows (runs, checks,
// stages, incidents), described by name. Internal bookkeeping such
// as artifact writes and run starts is left out; the trajectory page keeps the
// complete log.

import type { EventRecord, Message } from '@llmchip/kernel-api'
import type { Key } from '../i18n.ts'
import { CHECK_TITLES, RUN_TITLES, STEPS } from './flowSteps.ts'

type T = (key: Key, params?: Record<string, unknown>) => string

/** One readable line, or null for events the feed leaves out. */
export function activityLine(e: EventRecord, runNames: Map<string, string>, t: T, message: (m: Message) => string): { text: string; tone: '' | 'ok' | 'bad' } | null {
  const p = e.payload as Record<string, any>
  const runTitle = (plugin: string, handler: string) => {
    const key = RUN_TITLES[`${plugin}/${handler}`]
    return key ? t(key) : `${plugin}/${handler}`
  }
  switch (e.kind) {
    case 'run.queued':
      return { text: t('activity.runQueued', { run: runTitle(p.run.plugin, p.run.handler) }), tone: '' }
    case 'run.finished': {
      const run = runNames.get(p.run) ?? t('activity.aRun')
      return p.execution === 'exited_zero' ? { text: t('activity.runOk', { run }), tone: 'ok' } : { text: t('activity.runFailed', { run }), tone: 'bad' }
    }
    case 'verdict.recorded': {
      const v = p.verdict
      const check = CHECK_TITLES[v.check] ? t(CHECK_TITLES[v.check]!) : v.check
      return { text: t('activity.verdict', { check, verdict: t(`flow.verdict.${v.verdict}` as Key) }), tone: v.verdict === 'pass' ? 'ok' : 'bad' }
    }
    case 'stage.entered': {
      const step = STEPS.find((s) => s.stage === p.stage)
      return { text: t('activity.stage', { stage: `${p.stage}${step ? ` ${t(step.title)}` : ''}` }), tone: '' }
    }
    case 'project.created':
      return { text: t('activity.project', { project: p.project.title }), tone: '' }
    case 'incident.opened':
      return { text: t('activity.incident', { summary: message(p.incident.summary) }), tone: 'bad' }
    case 'kernel.started':
      return { text: t('activity.started'), tone: '' }
    case 'kernel.paused':
      return { text: t(p.paused ? 'activity.paused' : 'activity.resumed'), tone: p.paused ? 'bad' : '' }
    default:
      return null
  }
}

/** Run id → readable run title, from the run.queued events in view. */
export function runNamesOf(events: EventRecord[], t: T): Map<string, string> {
  const names = new Map<string, string>()
  for (const e of events) {
    if (e.kind !== 'run.queued') continue
    const run = (e.payload as { run: { id: string; plugin: string; handler: string } }).run
    const key = RUN_TITLES[`${run.plugin}/${run.handler}`]
    names.set(run.id, key ? t(key) : `${run.plugin}/${run.handler}`)
  }
  return names
}
