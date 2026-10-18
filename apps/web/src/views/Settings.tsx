import { useState } from 'react'
import { useKernel, useQuery } from '../api.ts'
import { useI18n } from '../i18n.ts'
import { ErrorLine, Section } from './common.tsx'

/** Settings (FR-UI-13): language, kernel, emergency stop, plugins, secrets. */
export function SettingsView() {
  const { t, lang, setLang } = useI18n()
  const { client } = useKernel()
  const { data: info } = useQuery((c) => c.call('kernel.info'), [])
  const { data: plugins } = useQuery((c) => c.call('plugins.list'), [], false)
  const { data: secrets, reload } = useQuery((c) => c.call('secrets.list'), [])
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  return (
    <>
      <Section title={t('settings.language')}>
        <div className="form inline">
          <button className={lang === 'zh' ? 'active' : ''} onClick={() => setLang('zh')} data-lang="zh">
            中文
          </button>
          <button className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')} data-lang="en">
            English
          </button>
        </div>
      </Section>
      <Section title={t('settings.emergency')}>
        <p>
          {t('common.state')}: <span className={info?.paused ? 'pill warn' : 'pill ok'}>{info?.paused ? t('settings.paused') : t('settings.running')}</span>
        </p>
        <div className="form inline">
          <button className="danger" disabled={info?.paused} onClick={() => client.call('kernel.pause', { reason: 'emergency stop from the UI' })}>
            {t('settings.pause')}
          </button>
          <button disabled={!info?.paused} onClick={() => client.call('kernel.resume')}>
            {t('settings.resume')}
          </button>
        </div>
      </Section>
      <Section title={t('settings.kernel')}>
        <dl className="facts">
          <dt>{t('settings.version')}</dt>
          <dd>
            harnessd {info?.version} (kernel-api {info?.apiVersion})
          </dd>
          <dt>{t('settings.events')}</dt>
          <dd>{info?.head}</dd>
        </dl>
      </Section>
      <Section title={t('settings.plugins')}>
        <table>
          <tbody>
            {plugins?.plugins.map((p) => (
              <tr key={p.id}>
                <td>
                  <strong>{p.id}</strong> {p.version}
                </td>
                <td>{p.runtime}</td>
                <td>{p.kinds.join(', ')}</td>
                <td className="muted">{[...p.handlers, ...p.data.map((d) => `data:${d.id}`)].join(' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section title={t('settings.secrets')}>
        <ul className="list compact">{secrets?.names.map((n) => <li key={n}>{n}</li>)}</ul>
        <form
          className="form inline"
          onSubmit={async (e) => {
            e.preventDefault()
            try {
              await client.call('secrets.set', { name, value })
              setName('')
              setValue('')
              reload()
            } catch (err) {
              setError((err as Error).message)
            }
          }}
        >
          <input placeholder={t('settings.secretName')} value={name} onChange={(e) => setName(e.target.value)} required />
          <input type="password" placeholder={t('settings.secretValue')} value={value} onChange={(e) => setValue(e.target.value)} required autoComplete="off" />
          <button type="submit">{t('settings.setSecret')}</button>
        </form>
        <ErrorLine error={error} />
      </Section>
    </>
  )
}
