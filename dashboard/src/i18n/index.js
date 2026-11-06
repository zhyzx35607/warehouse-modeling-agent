import { createI18n } from 'vue-i18n'
import zh from './zh-CN.js'
import en from './en.js'

const i18n = createI18n({
  legacy: false,
  locale: 'zh-CN',
  fallbackLocale: 'en',
  messages: { 'zh-CN': zh, en },
})

export default i18n
