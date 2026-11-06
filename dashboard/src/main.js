import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { MotionPlugin } from '@vueuse/motion'
import App from './App.vue'
import router from './router'
import i18n from './i18n/index.js'
import './styles/apple.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)
app.use(MotionPlugin)
app.mount('#app')
