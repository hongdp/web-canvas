/**
 * Convert structured TipTap HTML to clean Markdown text.
 */
export function htmlToMarkdown(html: string): string {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')
  
  function convertNode(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) {
      return node.textContent || ''
    }
    
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return ''
    }
    
    const element = node as HTMLElement
    const tagName = element.tagName.toUpperCase()
    
    let childrenContent = ''
    element.childNodes.forEach((child) => {
      childrenContent += convertNode(child)
    })
    
    switch (tagName) {
      case 'H1':
        return `# ${childrenContent}\n\n`
      case 'H2':
        return `## ${childrenContent}\n\n`
      case 'H3':
        return `### ${childrenContent}\n\n`
      case 'H4':
        return `#### ${childrenContent}\n\n`
      case 'P':
        return `${childrenContent}\n\n`
      case 'STRONG':
      case 'B':
        return `**${childrenContent}**`
      case 'EM':
      case 'I':
        return `*${childrenContent}*`
      case 'BLOCKQUOTE':
        return childrenContent
          .split('\n')
          .map((line) => line ? `> ${line}` : '>')
          .join('\n') + '\n\n'
      case 'PRE':
        // TipTap pre usually wraps code
        return `\`\`\`\n${element.innerText || childrenContent}\n\`\`\`\n\n`
      case 'CODE':
        if (element.parentElement?.tagName.toUpperCase() === 'PRE') {
          return childrenContent
        }
        return `\`${childrenContent}\``
      case 'UL':
        return `${childrenContent}\n`
      case 'OL':
        return `${childrenContent}\n`
      case 'LI': {
        const parentTag = element.parentElement?.tagName.toUpperCase()
        if (parentTag === 'OL') {
          const index = Array.from(element.parentElement!.children).indexOf(element) + 1
          return `${index}. ${childrenContent}\n`
        }
        return `- ${childrenContent}\n`
      }
      case 'BR':
        return '\n'
      case 'HR':
        return '---\n\n'
      case 'INS':
        return childrenContent // Keep additions normally
      case 'DEL':
        return '' // Exclude deletions from final markdown representation
      default:
        return childrenContent
    }
  }
  
  let markdown = ''
  doc.body.childNodes.forEach((child) => {
    markdown += convertNode(child)
  })
  
  return markdown.replace(/\n{3,}/g, '\n\n').trim()
}

/**
 * Convert TipTap HTML to formatted Plain Text.
 */
export function htmlToPlainText(html: string): string {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')
  
  // Strip deletion marks to prevent them appearing in plain text export
  const dels = doc.querySelectorAll('del')
  dels.forEach((del) => del.remove())
  
  return doc.body.innerText || doc.body.textContent || ''
}

/**
 * Helper to escape HTML characters to prevent XSS and tag breakout.
 */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

/**
 * Convert inline markdown syntax (bold, italic, code) to HTML tags.
 */
function parseInlineMarkdown(text: string): string {
  let escaped = escapeHtml(text)
  
  // Bold: **text** or __text__
  escaped = escaped.replace(/(\*\*|__)(.*?)\1/g, '<strong>$2</strong>')
  
  // Italic: *text* or _text_
  escaped = escaped.replace(/(\*|_)(.*?)\1/g, '<em>$2</em>')
  
  // Inline Code: `code`
  escaped = escaped.replace(/`(.*?)`/g, '<code>$1</code>')
  
  return escaped
}

/**
 * Convert Markdown text to structured HTML for TipTap compatibility.
 */
export function markdownToHtml(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  let html = ''
  let inList = false
  let listType: 'ul' | 'ol' | null = null
  let inBlockquote = false
  let inCodeBlock = false
  let codeContent: string[] = []
  let paragraphLines: string[] = []

  function flushParagraph() {
    if (paragraphLines.length > 0) {
      const content = paragraphLines.join(' ')
      if (content.trim()) {
        html += `<p>${parseInlineMarkdown(content)}</p>`
      }
      paragraphLines = []
    }
  }

  function flushList() {
    if (inList && listType) {
      html += `</${listType}>`
      inList = false
      listType = null
    }
  }

  function flushBlockquote() {
    if (inBlockquote) {
      html += '</blockquote>'
      inBlockquote = false
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmedLine = line.trim()

    // 1. Handle Code Blocks
    if (trimmedLine.startsWith('```')) {
      if (inCodeBlock) {
        // End of code block
        const escapedCode = escapeHtml(codeContent.join('\n'))
        html += `<pre><code>${escapedCode}</code></pre>`
        inCodeBlock = false
        codeContent = []
      } else {
        flushList()
        flushBlockquote()
        flushParagraph()
        inCodeBlock = true
      }
      continue
    }

    if (inCodeBlock) {
      codeContent.push(line)
      continue
    }

    // 2. Handle Blockquotes
    if (trimmedLine.startsWith('>')) {
      if (!inBlockquote) {
        flushList()
        flushParagraph()
        html += '<blockquote>'
        inBlockquote = true
      }
      const quoteText = line.substring(line.indexOf('>') + 1).replace(/^ /, '')
      paragraphLines.push(quoteText)
      continue
    } else if (inBlockquote && !trimmedLine.startsWith('>')) {
      flushParagraph()
      flushBlockquote()
    }

    // 3. Handle Horizontal Rules
    if (trimmedLine === '---' || trimmedLine === '***' || trimmedLine === '___') {
      flushList()
      flushBlockquote()
      flushParagraph()
      html += '<hr />'
      continue
    }

    // 4. Handle Headings
    const headingMatch = line.match(/^(#{1,6})\s+(.*)$/)
    if (headingMatch) {
      flushList()
      flushBlockquote()
      flushParagraph()
      const level = headingMatch[1].length
      const title = headingMatch[2]
      html += `<h${level}>${parseInlineMarkdown(title)}</h${level}>`
      continue
    }

    // 5. Handle Lists
    const ulMatch = line.match(/^\s*[-*+]\s+(.*)$/)
    const olMatch = line.match(/^\s*\d+\.\s+(.*)$/)

    if (ulMatch) {
      flushParagraph()
      if (inList && listType !== 'ul') {
        flushList()
      }
      if (!inList) {
        html += '<ul>'
        inList = true
        listType = 'ul'
      }
      html += `<li>${parseInlineMarkdown(ulMatch[1])}</li>`
      continue
    }

    if (olMatch) {
      flushParagraph()
      if (inList && listType !== 'ol') {
        flushList()
      }
      if (!inList) {
        html += '<ol>'
        inList = true
        listType = 'ol'
      }
      html += `<li>${parseInlineMarkdown(olMatch[1])}</li>`
      continue
    }

    // Empty lines act as block delimiters
    if (trimmedLine === '') {
      flushList()
      flushBlockquote()
      flushParagraph()
      continue
    }

    // Regular line, accumulate in paragraph
    paragraphLines.push(trimmedLine)
  }

  // Final flushes
  if (inCodeBlock) {
    const escapedCode = escapeHtml(codeContent.join('\n'))
    html += `<pre><code>${escapedCode}</code></pre>`
  }
  flushList()
  flushBlockquote()
  flushParagraph()

  return html
}

/**
 * Convert Plain Text to formatted HTML paragraphs.
 */
export function txtToHtml(text: string): string {
  const escaped = escapeHtml(text)
  return escaped
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0)
    .map((p) => `<p>${p.replace(/\n/g, '<br />')}</p>`)
    .join('\n')
}

/**
 * Sanitize untrusted HTML inputs client-side to prevent XSS.
 */
export function sanitizeHtml(html: string): string {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')
  
  // Remove script tags
  const scripts = doc.querySelectorAll('script')
  scripts.forEach((s) => s.remove())
  
  // Remove inline handlers and javascript: URIs
  const allElements = doc.querySelectorAll('*')
  allElements.forEach((el) => {
    const attrs = Array.from(el.attributes)
    attrs.forEach((attr) => {
      const name = attr.name.toLowerCase()
      if (name.startsWith('on') || attr.value.trim().toLowerCase().startsWith('javascript:')) {
        el.removeAttribute(attr.name)
      }
    })
  })
  
  // Imported HTML gets the same paragraph normalization as pasted HTML: web
  // pages routinely ship a chapter as one block of <br>-separated lines.
  return normalizeBrParagraphs(doc.body.innerHTML)
}

/**
 * Blocks holding at least this many <br> line breaks are treated as a "wall of
 * lines" (typical of content copied out of a web page) and split into real
 * paragraphs. A single isolated <br> is left alone: there it is usually a
 * deliberate soft break (address, verse, Shift+Enter).
 */
const MIN_BRS_TO_SPLIT = 2

/** Tags that may sit between a block and a <br> without ending the line flow. */
const INLINE_TAGS = new Set([
  'SPAN', 'STRONG', 'B', 'EM', 'I', 'U', 'S', 'A', 'FONT', 'SMALL', 'MARK',
  'SUB', 'SUP', 'CODE', 'ABBR', 'LABEL', 'Q', 'CITE', 'TIME', 'VAR', 'SAMP',
  'KBD', 'INS', 'DEL', 'BDI', 'BDO'
])

/** Anything that starts its own block box — a container holding one of these
 * is not a leaf and is left to its children to normalize. */
const BLOCK_SELECTOR =
  'p,div,blockquote,section,article,aside,main,header,footer,ul,ol,li,table,tr,td,th,h1,h2,h3,h4,h5,h6,pre,figure,figcaption'

/** Leaf blocks worth splitting. Headings and list items keep their <br>. */
const SPLITTABLE_SELECTOR = 'p,div,blockquote,section,article,td'

const MEDIA_SELECTOR = 'img,video,iframe,audio'

/** A node worth keeping as its own paragraph: it carries text or media. */
function carriesContent(node: Node): boolean {
  if ((node.textContent || '').trim().length > 0) return true
  if (node.nodeType === 1) {
    const el = node as Element
    // matches() must be checked too — querySelector only sees descendants, so
    // an image alone on a line would otherwise be dropped.
    return el.matches(MEDIA_SELECTOR) || el.querySelector(MEDIA_SELECTOR) !== null
  }
  if (node.nodeType === 11) {
    return (node as DocumentFragment).querySelector(MEDIA_SELECTOR) !== null
  }
  return false
}

/** The <br>s that end a line *of this container* — i.e. every element between
 * the <br> and the container is inline, so no nested block owns it. */
function lineBreaksOf(container: Element): Element[] {
  return Array.from(container.querySelectorAll('br')).filter((br) => {
    let e = br.parentElement
    while (e && e !== container && INLINE_TAGS.has(e.tagName)) e = e.parentElement
    return e === container
  })
}

/**
 * Cut a container into paragraphs at its line breaks, emptying it.
 *
 * Uses Range.extractContents, which clones any partially-selected inline
 * ancestors — that is what makes `<span>a<br>b</span>` become
 * `<p><span>a</span></p><p><span>b</span></p>` instead of losing the span or
 * being skipped entirely.
 */
function cutIntoParagraphs(doc: Document, container: Element): HTMLParagraphElement[] {
  const paragraphs: HTMLParagraphElement[] = []
  const wrap = (frag: DocumentFragment) => {
    const p = doc.createElement('p')
    p.appendChild(frag)
    return p
  }

  for (let guard = 0; guard < 10000; guard++) {
    const br = lineBreaksOf(container)[0]
    if (!br) break
    const range = doc.createRange()
    range.setStart(container, 0)
    range.setEndBefore(br)
    const frag = range.extractContents()
    br.remove()
    if (carriesContent(frag)) paragraphs.push(wrap(frag))
  }

  const rest = doc.createDocumentFragment()
  while (container.firstChild) rest.appendChild(container.firstChild)
  if (carriesContent(rest)) paragraphs.push(wrap(rest))

  return paragraphs
}

/**
 * Turn `<br>`-separated pseudo-paragraphs into real <p> blocks.
 *
 * Pasting from a web page yields a wall of <br> line breaks — sometimes inside
 * one block, sometimes wrapped in <span>s, sometimes as a bare fragment with no
 * block at all. That shape is bad for everything downstream: the chapter
 * becomes a single ProseMirror node (so any edit rebuilds all of it), diffs
 * cannot align on paragraphs, and the LLM sees one run-on block.
 *
 * Handled shapes: `<p>a<br>b</p>`, `<div><span>a<br>b</span></div>`, and bare
 * `a<br>b` with no wrapper. Runs of consecutive <br> collapse into one break,
 * empty groups never become empty paragraphs, and a lone <br> stays a soft
 * break. Nested blocks are left to their own pass, and headings/list items keep
 * their <br> (a break there is usually deliberate).
 *
 * Parsing happens in the inert document produced by DOMParser, which has no
 * browsing context — embedded <img> elements are therefore never loaded or
 * decoded (see the performance note in App.tsx).
 */
export function normalizeBrParagraphs(html: string): string {
  if (!/<br/i.test(html)) return html

  const doc = new DOMParser().parseFromString(html, 'text/html')

  // 1. Leaf blocks: split in place.
  for (const block of Array.from(doc.body.querySelectorAll(SPLITTABLE_SELECTOR))) {
    if (block.querySelector(BLOCK_SELECTOR)) continue // not a leaf; its children get their own pass
    if (lineBreaksOf(block).length < MIN_BRS_TO_SPLIT) continue

    const paragraphs = cutIntoParagraphs(doc, block)
    if (paragraphs.length < 2) {
      // Nothing gained — put the content back exactly where it was.
      paragraphs.forEach((p) => {
        while (p.firstChild) block.appendChild(p.firstChild)
      })
      continue
    }
    const frag = doc.createDocumentFragment()
    paragraphs.forEach((p) => frag.appendChild(p))
    block.replaceWith(frag)
  }

  // 2. Bare top-level runs: content pasted without any block wrapper. Existing
  //    block children pass through untouched; only inline runs are wrapped.
  const topBrs = Array.from(doc.body.childNodes).filter(
    (n) => n.nodeType === 1 && (n as Element).tagName === 'BR'
  )
  if (topBrs.length >= MIN_BRS_TO_SPLIT) {
    const groups: Node[][] = [[]]
    for (const node of Array.from(doc.body.childNodes)) {
      if (node.nodeType === 1 && (node as Element).tagName === 'BR') {
        if (groups[groups.length - 1].length > 0) groups.push([])
      } else {
        groups[groups.length - 1].push(node)
      }
    }
    const rebuilt = doc.createDocumentFragment()
    for (const group of groups) {
      if (!group.some(carriesContent)) continue
      const isBlockRun = group.some(
        (n) => n.nodeType === 1 && (n as Element).matches(BLOCK_SELECTOR)
      )
      if (isBlockRun) {
        group.forEach((n) => rebuilt.appendChild(n))
      } else {
        const p = doc.createElement('p')
        group.forEach((n) => p.appendChild(n))
        rebuilt.appendChild(p)
      }
    }
    doc.body.replaceChildren(rebuilt)
  }

  return doc.body.innerHTML
}

/**
 * Extract the first H1 element as the chapter title and strip it from the body content.
 */
export function extractChapterTitleFromContent(htmlContent: string, defaultTitle: string): { title: string; content: string } {
  const parser = new DOMParser()
  const doc = parser.parseFromString(htmlContent, 'text/html')
  const body = doc.body

  const firstChild = body.firstElementChild
  if (firstChild && firstChild.tagName.toUpperCase() === 'H1') {
    const extractedTitle = firstChild.textContent?.trim() || defaultTitle
    firstChild.remove()
    return {
      title: extractedTitle,
      content: body.innerHTML
    }
  }

  return {
    title: defaultTitle,
    content: htmlContent
  }
}

/**
 * Splits combined HTML draft into separate chapters.
 */
export function splitHtmlToChapters(html: string): { title: string; content: string }[] {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')
  const body = doc.body

  const chapters: { title: string; content: string }[] = []
  let currentContent = ''

  const children = Array.from(body.childNodes)

  for (const node of children) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const element = node as HTMLElement
      const tagName = element.tagName.toUpperCase()

      if (tagName === 'HR') {
        if (currentContent.trim()) {
          const { title, content } = extractChapterTitleFromContent(currentContent.trim(), 'Untitled Chapter')
          chapters.push({ title, content: sanitizeHtml(content) })
          currentContent = ''
        }
        continue
      }
      
      currentContent += element.outerHTML + '\n'
    } else if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent
      if (text) {
        currentContent += text
      }
    }
  }

  if (currentContent.trim()) {
    const { title, content } = extractChapterTitleFromContent(currentContent.trim(), 'Untitled Chapter')
    chapters.push({ title, content: sanitizeHtml(content) })
  }

  return chapters
}

/**
 * Splits combined Markdown draft into separate chapters.
 */
export function splitMarkdownToChapters(markdown: string): { title: string; content: string }[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const chapters: { title: string; content: string }[] = []
  
  let currentLines: string[] = []
  let inCodeBlock = false

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()

    if (trimmed.startsWith('```')) {
      inCodeBlock = !inCodeBlock
      currentLines.push(line)
      continue
    }

    if (inCodeBlock) {
      currentLines.push(line)
      continue
    }

    if (trimmed === '---' || trimmed === '***' || trimmed === '___') {
      if (currentLines.length > 0) {
        const htmlContent = markdownToHtml(currentLines.join('\n'))
        const { title, content } = extractChapterTitleFromContent(htmlContent, 'Untitled Chapter')
        chapters.push({ title, content })
        currentLines = []
      }
      continue
    }

    currentLines.push(line)
  }

  if (currentLines.length > 0) {
    const htmlContent = markdownToHtml(currentLines.join('\n'))
    const { title, content } = extractChapterTitleFromContent(htmlContent, 'Untitled Chapter')
    chapters.push({ title, content })
  }

  return chapters
}

/**
 * Splits combined Plain Text draft into separate chapters.
 */
export function splitTxtToChapters(text: string): { title: string; content: string }[] {
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  const chapters: { title: string; content: string }[] = []
  
  let currentTitle = ''
  let currentLines: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const trimmed = line.trim()
    
    const nextLine = lines[i + 1]
    const nextLineTrimmed = nextLine !== undefined ? nextLine.trim() : ''
    
    if (
      trimmed.length > 0 &&
      nextLineTrimmed.length > 0 && 
      /^[=]+$/.test(nextLineTrimmed) &&
      Math.abs(nextLineTrimmed.length - trimmed.length) <= 3
    ) {
      if (currentTitle || currentLines.length > 0) {
        const htmlContent = txtToHtml(currentLines.join('\n'))
        const finalTitle = currentTitle || 'Untitled Chapter'
        const { title, content } = extractChapterTitleFromContent(htmlContent, finalTitle)
        chapters.push({ title, content })
        currentLines = []
      }
      currentTitle = trimmed
      i++ // Skip divider line
      continue
    }

    currentLines.push(line)
  }

  if (currentTitle || currentLines.length > 0) {
    const htmlContent = txtToHtml(currentLines.join('\n'))
    const finalTitle = currentTitle || 'Untitled Chapter'
    const { title, content } = extractChapterTitleFromContent(htmlContent, finalTitle)
    chapters.push({ title, content })
  }

  return chapters
}



