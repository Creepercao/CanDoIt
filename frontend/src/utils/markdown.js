import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const md = new MarkdownIt({
  html: true,
  breaks: true,
  linkify: true,
  typographer: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs rounded-lg p-3 my-2 overflow-x-auto text-xs"><code>' +
          hljs.highlight(str, { language: lang, ignoreIllegals: true }).value +
          '</code></pre>'
      } catch {}
    }
    return '<pre class="hljs rounded-lg p-3 my-2 overflow-x-auto text-xs"><code>' +
      md.utils.escapeHtml(str) + '</code></pre>'
  },
})

// Auto-link bare URLs not already in markdown syntax
md.linkify.set({ fuzzyLink: false })

export function renderMarkdown(text) {
  if (!text) return ''
  return md.render(text)
}
