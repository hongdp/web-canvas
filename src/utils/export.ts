import { htmlToMarkdown, htmlToPlainText } from './convert'
import type { CanvasDocument } from '../store/useAppStore'

export function exportDocument(
  format: 'html' | 'markdown' | 'txt',
  exportAll: boolean,
  documents: CanvasDocument[],
  activeDoc: Pick<CanvasDocument, 'title' | 'content'>,
  bookTitle: string
) {
  const element = document.createElement('a')
  let content = ''
  let filename: string
  let mimeType: string

  const cleanBookTitle = bookTitle.trim().replace(/[/\\?%*:|"<>\s]+/g, '_').replace(/_+/g, '_') || 'Book'
  const cleanChapterTitle = activeDoc.title.trim().replace(/[/\\?%*:|"<>\s]+/g, '_').replace(/_+/g, '_') || 'Chapter'
  const baseFilename = exportAll
    ? cleanBookTitle
    : `${cleanBookTitle}_${cleanChapterTitle}`

  if (format === 'html') {
    let bodyContent = ''
    if (exportAll) {
      documents.forEach((doc, idx) => {
        const contentTrimmed = doc.content.trim()
        const startsWithH1 = contentTrimmed.startsWith('<h1') || contentTrimmed.startsWith('<h1>')
        if (startsWithH1) {
          bodyContent += `${doc.content}\n`
        } else {
          bodyContent += `<h1>${doc.title}</h1>\n${doc.content}\n`
        }
        if (idx < documents.length - 1) {
          bodyContent += `<hr style="margin: 3rem 0; border: none; border-top: 1px solid #cbd5e1;" />\n`
        }
      })
    } else {
      bodyContent = activeDoc.content
    }

    content = `<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>${exportAll ? 'All Chapters Combined' : activeDoc.title}</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: #1e293b; max-width: 740px; margin: 2rem auto; padding: 0 1rem; }
    h1, h2, h3 { color: #0f172a; }
    blockquote { border-left: 4px solid #f59e0b; padding-left: 1rem; font-style: italic; color: #475569; }
    pre { background-color: #f1f5f9; padding: 1rem; border-radius: 6px; overflow-x: auto; }
    code { font-family: monospace; background-color: #f1f5f9; padding: 0.2rem 0.4rem; border-radius: 4px; }
  </style>
</head>
<body>
  ${bodyContent}
</body>
</html>`
    filename = `${baseFilename}.html`
    mimeType = 'text/html'
  } else if (format === 'markdown') {
    if (exportAll) {
      documents.forEach((doc, idx) => {
        const markdownContent = htmlToMarkdown(doc.content)
        const startsWithH1 = markdownContent.trim().startsWith('# ')
        if (startsWithH1) {
          content += `${markdownContent}\n`
        } else {
          content += `# ${doc.title}\n\n${markdownContent}\n`
        }
        if (idx < documents.length - 1) {
          content += `\n---\n\n`
        }
      })
    } else {
      content = htmlToMarkdown(activeDoc.content)
    }
    filename = `${baseFilename}.md`
    mimeType = 'text/markdown'
  } else {
    if (exportAll) {
      documents.forEach((doc, idx) => {
        const plainTextContent = htmlToPlainText(doc.content)
        const lines = plainTextContent.trim().split('\n')
        const startsWithHeading = lines.length > 1 && lines[1].trim().length > 0 && /^[=]+$/.test(lines[1].trim())
        if (startsWithHeading) {
          content += `${plainTextContent}\n`
        } else {
          content += `${doc.title}\n${'='.repeat(doc.title.length)}\n\n${plainTextContent}\n`
        }
        if (idx < documents.length - 1) {
          content += `\n\n\n`
        }
      })
    } else {
      content = htmlToPlainText(activeDoc.content)
    }
    filename = `${baseFilename}.txt`
    mimeType = 'text/plain'
  }

  const file = new Blob([content], { type: mimeType })
  element.href = URL.createObjectURL(file)
  element.download = filename
  document.body.appendChild(element)
  element.click()
  document.body.removeChild(element)
}
