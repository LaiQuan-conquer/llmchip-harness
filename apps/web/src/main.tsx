import { StrictMode, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App.tsx'
import { KernelContext, useKernelConnection } from './api.ts'
import { I18nContext, makeI18n, type Lang } from './i18n.ts'
import './styles.css'

function Root() {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem('llmchip.lang') as Lang) ?? (navigator.language.startsWith('zh') ? 'zh' : 'en'))
  const setLang = (next: Lang) => {
    localStorage.setItem('llmchip.lang', next)
    document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en'
    setLangState(next)
  }
  const i18n = useMemo(() => makeI18n(lang, setLang), [lang])
  const { kernel, error } = useKernelConnection()
  return (
    <I18nContext.Provider value={i18n}>
      {kernel ? (
        <KernelContext.Provider value={kernel}>
          <App />
        </KernelContext.Provider>
      ) : (
        <div className="splash">
          <h1>{i18n.t('app.title')}</h1>
          <p>{error === 'missing-token' ? i18n.t('conn.missingToken') : error ? i18n.t('conn.failed') + error : i18n.t('conn.connecting')}</p>
        </div>
      )}
    </I18nContext.Provider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
