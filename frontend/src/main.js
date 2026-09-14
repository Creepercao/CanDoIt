import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './assets/tailwind.css'
// 明亮主题的代码高亮（highlight.js 浅色配色）
import 'highlight.js/styles/github.css'

const app = createApp(App)
app.use(createPinia())
app.mount('#app')
