/**
 * TypeScript ↔ Python parity fixtures (backend_authority.md phase 2).
 *
 * Every case here is run through the TypeScript implementation and its
 * output recorded in scripts/parity/fixtures/<module>.json; scripts/
 * test_parity.py runs the same inputs through the Python port and must match
 * byte for byte. The TypeScript is the specification.
 *
 * Normally this test CHECKS that the committed fixtures still equal what the
 * TypeScript produces, so a change to a ported function cannot land without
 * regenerating them (and re-running the Python side). To regenerate:
 *   WRITE_FIXTURES=1 npx vitest run src/parity    (npm run parity:fixtures)
 */
import { describe, it, expect } from 'vitest'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { paragraphBlocks, anchorMatches, asItems, rewriteLoss, rewriteLossNote, blockText, topLevelBlocks, chapterParagraphs, chapterChars, numberedLine, searchableText, blockImages, paragraphSpans, numberedParagraphSpans, isPlainChapterHtml, isTextRewritableHtml, stripImageMarkerParagraphs, asBlocks, applyParagraphEdits, type ParagraphEdit } from '../../utils/paragraphs'
import { diffHtml, stripDiffMarkup } from '../../utils/diff'
import { resolveDiffMarkupInHtml } from '../../utils/diffResolution'
import {
  stripIncompleteEndTag, chapterAttribute, newChapterAttribute, extractTaggedBlock, hasElisionMarkers, validateCanvasReplacement,
  parseEditBlocks, stripStrayDocumentMarkup, parseAssistantResponse, applyEditBlocks, applyEditBlocksLocally, stripBlankParagraphs,
  parseDocStatus, stripDocStatus, detectFailedDocumentUpdate, trimIncompleteHtmlTail, isBlankContent, type EditBlock, claimsOwnWrite } from '../../utils/text'
import { supportedReasoningEfforts, resolveReasoningEffort, reasoningBudgetTokens, anthropicThinking, geminiThinking, moonshotThinking } from '../../utils/reasoningEffort'
import { hashContent, planLedgerTurn, ledgerChapterIds, orderAdmissionsByStability, type ContextLedger, type LedgerDocLike, type LedgerEntry } from '../../utils/contextLedger'
import { resolveDocumentProtocol, type DocumentProtocol } from '../../utils/protocolChoice'
import { leadingH1Text, contentWithRenamedHeading } from '../../utils/titleSync'
import { splitStreamingResponse, buildCompletionWarnings, NO_ACTION_RETRY_INSTRUCTION, MAX_NO_ACTION_RETRIES, ASSISTANT_PLACEHOLDER, INTERRUPTED_NOTICE, RECONNECT_FAILED_NOTICE, STEP_LIMIT_NOTE } from '../../hooks/chat/streamHandlers'

const FIXTURES = resolve(dirname(fileURLToPath(import.meta.url)), '../../../scripts/parity/fixtures')
const WRITE = process.env.WRITE_FIXTURES === '1'

/** Diff group ids are random; both sides compare them renumbered in order of appearance. */
export function normalizeDiffIds(html: string): string {
  const seen = new Map<string, string>()
  return html.replace(/data-diff-id="([^"]*)"/g, (_m, id: string) => {
    if (!seen.has(id)) seen.set(id, `diff-${seen.size + 1}`)
    return `data-diff-id="${seen.get(id)}"`
  })
}

// ── Shared inputs ─────────────────────────────────────────────────────────

const CHAPTER = '<h1>第三章 风起</h1><p>阿青推开门，<strong>风</strong>从巷口灌进来。</p><p>她想：&ldquo;该走了。&rdquo;</p><blockquote><p>门外无人。</p></blockquote><ul><li>一</li><li>二 &amp; 三</li></ul><p><img src="data:image/png;base64,AAAA" alt="x"></p><p>{{IMAGE_PLACEHOLDER_0}}</p><p></p>   loose text here <hr><p>a&nbsp;b<br>c</p>'
const ENGLISH = '<h2>Title</h2><p>The cat sat on the mat.</p><p>It was <em>warm</em>, and &lt;quiet&gt; &quot;outside&quot; &#39;then&#39;.</p>'
const DIFFED = '<p>before <ins class="diff-addition" data-diff-id="diff-a">added</ins> after</p><p><del class="diff-deletion" data-diff-id="diff-b">gone</del>kept text</p><p><ins class="diff-addition" data-diff-id="diff-c">whole new paragraph</ins></p><ul><li><ins class="diff-addition" data-diff-id="diff-d">new item</ins></li></ul><p>old <del class="diff-deletion" data-diff-id="diff-e">word</del><ins data-diff-id="diff-e" class="diff-addition">term</ins> here</p><p><ins class="diff-addition" data-diff-id="diff-f">X </ins>A<ins class="diff-addition" data-diff-id="diff-f"> Y</ins></p>'
const paras = (changed: boolean) => Array.from({ length: 40 }, (_, i) => `<p>Paragraph ${i} ${i === 20 && changed ? 'CHANGED' : 'same'} text that goes on for a while to make tokens.</p>`).join('')

interface Case { input: unknown[]; output: unknown }
interface Module { module: string; cases: Record<string, Case[]> }


/** A character card: fields as list entries under each heading. */
const CARD = '<h1>人物卡</h1><p>适用范围：第1–20章</p><h2>林霜</h2><ul><li><p>身份：港务局引航员，三级。</p></li><li><p>资质：甲等。</p></li></ul><h2>沈月</h2><ul>\n<li><p>身份：港务局局长。</p><ul><li>嵌套</li></ul></li>\n</ul>'

/** A chapter with a heading, inline markup and an image, for the paragraph-span cases. */
const SPANNED = '<h1>T</h1>\n<p>One a</p><p>Two <em>b</em></p> <p><img src="x.png"></p><p>Three</p>'



const run = <A extends unknown[]>(fn: (...a: A) => unknown, inputs: A[], post: (out: unknown) => unknown = o => o): Case[] =>
  // `undefined` would vanish from the JSON; Python's None is JSON null.
  inputs.map(input => ({ input, output: post(fn(...input)) ?? null }))

const EDIT = (search: string, replace: string, chapter?: string) =>
  `<edit${chapter ? ` chapter="${chapter}"` : ''}>\n<<<<<<< SEARCH\n${search}\n=======\n${replace}\n>>>>>>> REPLACE\n</edit>`
const RESPONSES: string[] = [
  'Just chat, no tags.\n<doc_status>unchanged</doc_status>',
  `Sure.\n<canvas>\n<h1>T</h1><p>a</p>\n</canvas>\n<doc_status>updated</doc_status>`,
  `Rewriting 3.\n<canvas chapter="3"><p>three</p></canvas>\nDone.\n<doc_status>updated</doc_status>`,
  `New chapter.\n<canvas new_chapter="第二章 进城"><p>进城</p></canvas>`,
  `<canvas><p>cut off`,
  `Edit.\n${EDIT('<p>a</p>', '<p>b</p>')}\nAnd more.\n<doc_status>updated</doc_status>`,
  `${EDIT('<p>a</p>', '<p>b</p>', '2')}${EDIT('<p>c</p>', '', '2')}`,
  `<selection_replace>new words</selection_replace>\n${EDIT('<p>x</p>', '<p>y</p>')}`,
  `${EDIT('<p>x</p>', '<p>y</p>')}\n<selection_replace>sel</selection_replace>`,
  `<canvas><p>active</p></canvas>\n${EDIT('<p>o</p>', '<p>p</p>', '1')}`,
  `<canvas><p>active</p></canvas>\n${EDIT('<p>o</p>', '<p>p</p>')}`,
  `<selection_replace>sel</selection_replace><canvas><p>stray</p></canvas>`,
  `<canvas><p>one</p></canvas><canvas chapter="4"><p>four</p></canvas><canvas new_chapter="五"><p>五</p></canvas>`,
  'chat <<<<<<< SEARCH\nunterminated',
  '<<<<<<< SEARCH\n<p>bare</p>\n=======\n<p>markers</p>\n>>>>>>> REPLACE',
  '<edit>\n<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n</edit>\n<edit>\n<<<<<<< SEARCH\n<p>c</p>\n=======\n<p>d</p>\n</edit>',
  '<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>b</p>\n<<<<<<< SEARCH\n<p>c</p>\n=======\n<p>d</p>\n>>>>>>> REPLACE',
  '<<<<<<< SEARCH\n<p>a</p>\n=======\n<p>half',
  '<<<<<<< SEARCH\n   \n=======\n<p>blank search</p>\n>>>>>>> REPLACE',
  'Canvas fenced.\n<canvas>\n```html\n<p>fenced</p>\n```\n</canvas>',
  '<CANVAS data-x="1"><p>caps</p></CANVAS >',
  '我已经把第二章改好了。\n<doc_status>unchanged</doc_status>',
  '你已经把第二章改好了。\n<doc_status>unchanged</doc_status>',
  "I've updated the paragraph.\n<doc_status>unchanged</doc_status>",
  'Here is the text.\n<doc_status>updated</doc_status>',
  'No declaration here.',
  '<edit>broken',
  '',
  'Streaming…\n<doc_sta',
  'Streaming…\n<doc_status>upd',
  'Streaming…\n<doc_status>updated</doc_st',
  'Prose with < less-than and <b>tag</b> end'
]
const DOC_PENDING = '<p>intro</p><p>keep <ins class="diff-addition" data-diff-id="d1">added words</ins> tail</p><p>second para here</p><ul><li>one</li><li>two</li></ul><p>中文段落在这里。</p>'
const LOCAL_EDITS: Array<[string, EditBlock[]]> = [
  [DOC_PENDING, [{ search: '<p>second para here</p>', replace: '<p>second paragraph here</p>' }]],
  [DOC_PENDING, [{ search: '<p>keep <ins class="diff-addition" data-diff-id="d1">added words</ins> tail</p>', replace: '<p>keep <ins class="diff-addition" data-diff-id="d1">added words</ins> tails</p>' }]],
  [DOC_PENDING, [{ search: 'added words', replace: 'added word' }]],
  [DOC_PENDING, [{ search: '中文段落在这里。', replace: '中文段落不在这里。' }, { search: '<p>intro</p>', replace: '<p>introduction</p>' }]],
  [DOC_PENDING, [{ search: '<li>two</li>', replace: '<li>two</li><li>three</li>' }]],
  [DOC_PENDING, [{ search: '<p>missing</p>', replace: '<p>x</p>' }]],
  ['<p>a &amp; b</p>', [{ search: '<p>a &amp; b</p>', replace: '<p>a &amp; c</p>' }]],
  ['<p>one <em>two</em> three</p>', [{ search: '<p>one <em>two</em> three</p>', replace: '<p>one <strong>two</strong> three</p>' }]],
  ['<p>gone <del class="diff-deletion" data-diff-id="d2">deleted words</del> here</p>', [{ search: 'deleted words', replace: 'other words' }]],
  ['<p>Alpha beta gamma delta.</p>', [{ search: '<p>Alpha beta gamma delta.</p>', replace: '<p>Alpha beta GAMMA delta.</p>' }]]
]
const APPLY_EDITS: Array<[string, EditBlock[]]> = [
  ['<p>Hello world</p>', [{ search: '<p>Hello world</p>', replace: '<p>Hi</p>' }]],
  ['<p>real content</p>', [{ search: '<p>hallucinated content</p>', replace: '<p>X</p>' }]],
  ['<p>a</p>\n  <p>b</p>', [{ search: '<p>a</p>\n<p>b</p>', replace: '<p>c</p>' }]],
  ['<p>a</p>', [{ search: '  <p>a</p>\n', replace: '<p>b</p>' }]],
  ['<p>a</p><p>b</p><p>c</p>', [{ search: '<p>a</p>', replace: '<p>A</p>' }, { search: '<p>c</p>', replace: '<p>C</p>' }, { search: '<p>zz</p>', replace: '' }]],
  ['<p>cost $5</p>', [{ search: '<p>cost $5</p>', replace: "<p>cost $1 and $& and $' and $$</p>" }]],
  ['<p>a&nbsp;b</p>', [{ search: '<p>a b</p>', replace: '<p>c</p>' }]],
  ['<p>“quoted” and ‘single’</p>', [{ search: '<p>"quoted" and \'single\'</p>', replace: '<p>q</p>' }]],
  ['<p>fish &amp; chips</p>', [{ search: '<p>fish & chips</p>', replace: '<p>f</p>' }]],
  ['<p>it&#39;s</p>', [{ search: '<p>it’s</p>', replace: '<p>its</p>' }]],
  ['<p>one <strong>two</strong> three</p>', [{ search: '<p>one two three</p>', replace: '<p>1 2 3</p>' }]],
  ['<p class="x">one</p><p>two</p><p>three</p>', [{ search: '<p>one</p><p>two</p>', replace: '<p>12</p>' }]],
  ['<p>“Hello,” she said.</p><p>“Bye,” he said.</p>', [{ search: '<p>“Hello, she said.</p><p>“Bye, he said.</p>', replace: '<p>x</p>' }]],
  ['<p>“Same” text</p><p>“Same” text</p>', [{ search: '<p>Same text</p>', replace: '<p>x</p>' }]],
  ['<p>one</p><p>Lead-in clause, then the tail sentence to change.</p><p>three</p>', [{ search: '<p>then the tail sentence to change.</p>', replace: '<p>then a better tail.</p>' }]],
  ['<p>Opening words here, and the rest of it stays.</p>', [{ search: '<p>Opening words here,</p>', replace: '<p>New opening,</p>' }]],
  ['<p>Keep this sentence. Rewrite this part please.</p>', [{ search: '<p>Rewrite this part please.</p>', replace: '<p>First new.</p><p>Second new.</p>' }]],
  ['<p>Keep this sentence. Remove this one please.</p>', [{ search: '<p>Remove this one please.</p>', replace: '' }]],
  ['<p>Alpha one. Alpha two.</p><p>Beta one. Beta two.</p>', [{ search: '<p>Alpha two.</p><p>Beta one.</p>', replace: '<p>Joined text.</p>' }]],
  ['<p>Lead <strong>bold excerpt text</strong> end.</p>', [{ search: '<p>bold excerpt text</p>', replace: '<p>new excerpt words</p>' }]],
  ['<p>First: same phrase here.</p><p>Second: same phrase here.</p>', [{ search: '<p>same phrase here.</p>', replace: '<p>X</p>' }]],
  ['<p>The opening clause stays here. The closing clause.</p>', [{ search: '<p>The closing clause.</p>', replace: '<p>The opening clause stays here, joined with the closing clause.</p>' }]],
  ['<p>Lead <strong>bold excerpt text</strong> end.</p>', [{ search: '<p>bold excerpt text</p>', replace: '<p>A.</p><p>B.</p>' }]],
  ['<p>Some lead text, then the excerpt to retitle.</p>', [{ search: '<p>then the excerpt to retitle.</p>', replace: '<h2>A heading</h2>' }]],
  ['<p>a big dog ran far</p>', [{ search: '<p>dog</p>', replace: '<p>cat</p>' }]],
  ['<p><a title="hidden tooltip words">link</a> rest</p>', [{ search: '<p>hidden tooltip words</p>', replace: '<p>X</p>' }]],
  [CHAPTER, [{ search: '<p>她想：&ldquo;该走了。&rdquo;</p>', replace: '<p>她想：“留下。”</p>' }, { search: '<li>二 &amp; 三</li>', replace: '<li>二和三</li>' }]],
  ['<p>x (a.b) [c] {d} *e* +f? ^g$ |h| \\i</p>', [{ search: '<p>x (a.b) [c] {d} *e* +f? ^g$ |h| \\i</p>', replace: '<p>ok</p>' }]]
]

// Books. Fields the index, the ledger and the selector read; the same rows feed all three.
const RENDER = (id: string, kind: string) => `[${kind}:${id}]`
const LDOCS: LedgerDocLike[] = [{ id: 'd1', chars: 100, hash: 'h1' }, { id: 'd2', chars: 2000, hash: 'h2' }, { id: 'd3', chars: 300, hash: 'h3' }, { id: 'd4', chars: 400, hash: 'h4' }]
const L = (id: string, hash: string, chars: number, extra: Partial<LedgerEntry> = {}): LedgerEntry => ({ id, hash, chars, ...extra })
const LEDGER_123: ContextLedger = { entries: [L('d1', 'h1', 100), L('d2', 'h2', 2000), L('d3', 'h3', 300)] }
/** planLedgerTurn with a deterministic renderer in place of a function the JSON cannot carry. */
const planLedger = (current: ContextLedger, desired: string[], docs: LedgerDocLike[], active: string | null, o?: { render?: boolean; maxStaleChars?: number; keepIds?: string[] }) =>
  planLedgerTurn(current, desired, docs, active, { ...(o?.render ? { render: RENDER } : {}), ...(o?.maxStaleChars !== undefined ? { maxStaleChars: o.maxStaleChars } : {}), ...(o?.keepIds ? { keepIds: o.keepIds } : {}) })




const MODULES: Module[] = [
  {
    module: 'text',
    cases: {
      strip_incomplete_end_tag: run(stripIncompleteEndTag, [['abc</selection_replace>'], ['abc</selection_re'], ['abc e>'], ['</sel in middle'], [''], ['</selection_replace>']]),
      chapter_attribute: run(chapterAttribute, [['<canvas chapter="3">'], ["<edit chapter='第二章'>"], ['<canvas new_chapter="x">'], ['<canvas>'], ['<canvas chapter="  ">'], ['<canvas CHAPTER = "4" >']]),
      new_chapter_attribute: run(newChapterAttribute, [['<canvas new_chapter="第二章 进城">'], ["<canvas new_chapter=' 尾声 '>"], ['<canvas new_chapter="">'], ['<canvas chapter="3">']]),
      extract_tagged_block: run(extractTaggedBlock, [['no tags', 'canvas'], ['before <canvas><p>x</p></canvas> after', 'canvas'], ['<canvas><p>cut', 'canvas'], ['<Canvas foo="bar"><p>x</p></canvas >', 'canvas'], ['<canvas>\n```html\n<p>f</p>\n```\n</canvas>', 'canvas'], ['a <selection_replace>s</selection_replace> b', 'selection_replace'], [RESPONSES[2], 'canvas'], [RESPONSES[3], 'canvas']]),
      has_elision_markers: run(hasElisionMarkers, [['<p>第1–9章：见原条目。</p><p>第10章：新</p>'], ['<p>（略）</p>'], ['<p>略。</p>'], ['<li>第五章：同上</li>'], ['<p>此处省略三千字</p>'], ['<p>其余内容不变</p>'], ['<p>【下同】</p>'],
        ['<p>他的脸色保持不变，略微一笑。</p><p>她说……</p>'], ['<p>他省略了寒暄：“同上次一样。”</p>'], ['<p>余同学走了过来。</p>'], ['<p>数值：不变量</p>'], ['<p>a</p><!-- rest unchanged --><p>z</p>'], ['<p>[content continues]</p>'], ['<p>(rest of the document remains the same)</p>'], ['<p>...</p>'], ['<p>She paused... and went on.</p>'], ['<p>The story continues in the next room.</p>'], ['<p>ordinary</p>'], ['<p>[TRUNCATED]</p>']]),
      validate_canvas_replacement: run(validateCanvasReplacement, [['<p>x</p>', false], ['<p>[unchanged]</p>', true], ['<p>x</p>', true], ['<p>[unchanged]</p>', false]]),
      parse_edit_blocks: run(parseEditBlocks, RESPONSES.map(r => [r] as [string])),
      strip_stray_document_markup: run(stripStrayDocumentMarkup, RESPONSES.map(r => [r] as [string])),
      parse_assistant_response: run(parseAssistantResponse, RESPONSES.map(r => [r] as [string])),
      apply_edit_blocks: run(applyEditBlocks, APPLY_EDITS),
      apply_edit_blocks_locally: run(applyEditBlocksLocally, LOCAL_EDITS, o => ({ ...(o as object), html: normalizeDiffIds((o as { html: string }).html) })),
      strip_blank_paragraphs: run(stripBlankParagraphs, [['<p></p><p> </p><p>&nbsp;</p><p><br></p><p>x</p>\n\n<p>y</p>'], ['<ul><li>a</li>\n<li>b</li></ul>\n<h2>t</h2>'], ['<p>keep  inside</p>']]),
      parse_doc_status: run(parseDocStatus, RESPONSES.map(r => [r] as [string])),
      strip_doc_status: run(stripDocStatus, RESPONSES.map(r => [r] as [string])),
      detect_failed_document_update: run(detectFailedDocumentUpdate, RESPONSES.map(r => [r] as [string])),
      claims_own_write: run(claimsOwnWrite, [['I have rewritten chapter 1.'], ['我已经把第三章改好了'], ['你已经把第二章改好了'], ['Here is the updated text'], ['Does this read well?'], ['']]),
      trim_incomplete_html_tail: run(trimIncompleteHtmlTail, [['<p>The sleek ta'], ['<p>a</p><h'], ['<p>a &nbs'], ['<p>a</p><p>unclosed'], ['<p>fish & chips are good today</p>'], [''], ['<p>a &amp</p>']]),
      is_blank_content: run(isBlankContent, [[''], ['<p></p>'], ['<p>&nbsp; </p>'], ['<p>x</p>'], ['<p><img src="a"></p>'], ['<h1></h1><p>\n</p>']])
    }
  },
  {
    module: 'paragraphs',
    cases: {
      rewrite_loss: run(rewriteLoss, [
        ['<p>' + '甲'.repeat(300) + '</p>', '<p>' + '甲'.repeat(250) + '</p>'],
        ['<p>' + '甲'.repeat(300) + '</p>', '<p>' + '甲'.repeat(260) + '</p>'],
        ['<h2>A</h2><ul><li>' + '一'.repeat(100) + '</li><li>' + '二'.repeat(100) + '</li><li>三</li></ul>', '<h2>A</h2><ul><li>' + '一'.repeat(250) + '</li></ul>'],
        ['<h1>T</h1><h2>A</h2><p>' + 'x'.repeat(300) + '</p>', '<h1>T</h1><p>' + 'x'.repeat(320) + '</p>'],
        ['<p>short</p>', ''],
        ['<p>' + 'a'.repeat(400) + '</p>', '<p>' + 'a'.repeat(900) + '</p>']
      ]),
      rewrite_loss_note: run(rewriteLossNote, [['#3 "时间线"', { before: 4000, after: 3000, lostHeadings: 0, lostItems: 12 }], ['#1 "x"', { before: 300, after: 300, lostHeadings: 1, lostItems: 0 }], ['#2 "y"', { before: 1000, after: 995, lostHeadings: 2, lostItems: 3 }]]),
      block_text: run(blockText, [['<p>a&nbsp;b<br>c</p>'], ['<ul><li>x</li><li>y &amp; z</li></ul>'], ['<p>  tail \t\n</p>'], ['plain']]),
      top_level_blocks: run(topLevelBlocks, [[CHAPTER], [ENGLISH], [''], ['loose only'], ['<p>a</p> between <p>b</p>'], ['<p>un &amp; closed'], ['<img src="x"><p>after</p>'], ['<p><img src="y"/></p>text &amp; more']]),
      chapter_paragraphs: run(chapterParagraphs, [[CHAPTER], [ENGLISH], ['<p>{{IMAGE_PLACEHOLDER_3}}</p>'], ['<h1>标题</h1><p>一。</p><p></p><p><img src="x"></p><ul><li>a</li><li>b</li></ul>loose'], [DIFFED]]),
      chapter_chars: run(chapterChars, [[CHAPTER], [ENGLISH], ['']]),
      numbered_line: run(numberedLine, chapterParagraphs('<h2>T</h2><p>a &amp; b</p><p><img src="x"></p><ul><li>x</li><li>y</li></ul><p>words then a picture <img src="y"></p><p>{{IMAGE_PLACEHOLDER_0}}</p><p>tail {{IMAGE_PLACEHOLDER_1}}</p>').map(p => [p] as [typeof p])),
      searchable_text: run(searchableText, chapterParagraphs('<p>t</p><p><img src="x"></p><p>t <img src="y"></p>').map(p => [p] as [typeof p])),
      block_images: run(blockImages, [['<p>a <img src="x" alt="1"> b <img src="y"></p>'], ['<p>{{IMAGE_PLACEHOLDER_3}} x</p>'], ['<p>none</p>']]),
      paragraph_spans_card: run(paragraphSpans, [[CARD]]),
      numbered_paragraph_spans_card: run(numberedParagraphSpans, [[CARD]]),
      paragraph_spans: run(paragraphSpans, [[CHAPTER], [ENGLISH], [''], [SPANNED], ['loose <p>a</p> tail'], ['<!-- c --><p>a</p></p><div><div>x</div></div><br/><p class="x>y">q</p>'], ['<p>never closed'], ['<p>a<p>b</p>']]),
      numbered_paragraph_spans: run(numberedParagraphSpans, [[CHAPTER], [ENGLISH], [SPANNED], ['<p>a<p>b</p>'], ['<p>x &#8217; y</p>'], ['']]),
      is_plain_chapter_html: run(isPlainChapterHtml, [[CHAPTER], [ENGLISH], [SPANNED], ['<h1>标题</h1><p>一。</p><p>二<br>三</p><p></p>'], ['<p class="a">x</p>'], ['<p>x</p><ul><li>a</li></ul>'], ['loose'], ['']]),
      is_text_rewritable_html: run(isTextRewritableHtml, [[CHAPTER], ['<p>一。</p><p><img src="a" width="600" style=""> <img src="b"></p>'], ['<p>一。</p><p><img src="a">图</p>'], ['<p><br><img src="a"><br></p>'], ['<p class="x"><img src="a"></p>'], ['<p>x</p><ul><li>a</li></ul>'], ['']]),
      strip_image_marker_paragraphs: run(stripImageMarkerParagraphs, [['<p>一。</p><p>[image]</p><p>[image] [image]</p><p>见[image]</p>'], ['<p>[image]x</p>'], ['']]),
      as_blocks: run(asBlocks, [['<p>x</p>'], ['  <h2>t</h2> '], ['one\n\ntwo\nlines'], ['plain'], ['  ']]),
      paragraph_blocks: run(paragraphBlocks, [[CARD], ['<ol><li>a</li>x<li>b</li></ol><p>c</p>'], ['<ul></ul>']]),
      anchor_matches: run(anchorMatches, [['身份：港务局引航员，三级', '身份：港务局引杭员'], ['身份：港务局引航员', '身份：港务居引杭员'], ['abc', 'abd'], ['适用范围：第1–20章，主要', '适用范围：第1–25章，主要角色'], ['The quick brown fox jumps', 'The quikc brown fox'], ['短', '短'], ['• 第八章 渡口：阿林替朋友买了礼物', '第八章 渡口'], ['• 第八章 渡口：阿林替朋友买了礼物', '• 第八章 渡口'], ['•  第八章', '第八章'], ['第八章 渡口', '第九章']]),
      as_items: run(asItems, [['<p>x</p>'], ['<li>y</li>'], ['plain']]),
      apply_paragraph_edits: run(applyParagraphEdits, [
        [SPANNED, [{ paragraph: 2, action: 'replace', html: '<p>Uno</p>', startsWith: 'One' }, { paragraph: 5, action: 'insert_after', html: 'Four\n\nFive', startsWith: '¶5 Three' }, { paragraph: 4, action: 'delete', startsWith: '[image]' }]],
        [SPANNED, [{ paragraph: 1, action: 'insert_before', html: '<p>pre</p>', startsWith: '# T' }, { paragraph: 1, action: 'replace', html: '<h1>New</h1>', startsWith: 'T' }, { paragraph: 2, action: 'insert_before', html: '<p>mid</p>', startsWith: 'One' }]],
        [SPANNED, [{ paragraph: 3, action: 'replace', html: 'x', startsWith: 'Nope' }, { paragraph: 9, action: 'delete', startsWith: 'x' }]],
        // A replaced paragraph keeps its inline image; a replacement of its own with an image keeps that one only.
        ['<p>Intro.</p><p>She smiled. <img src="pic"></p><p>End.</p>', [{ paragraph: 2, action: 'replace', html: '<p>She grinned.</p>', startsWith: 'She smiled' }]],
        ['<p>Intro.</p><p>She smiled. <img src="pic"></p>', [{ paragraph: 2, action: 'replace', html: 'Plain words', startsWith: 'She smiled' }]],
        ['<p>Intro.</p><p>She smiled. <img src="pic"></p>', [{ paragraph: 2, action: 'replace', html: '<p>Own <img src="other"></p>', startsWith: 'She smiled' }]],
        [SPANNED, [{ paragraph: 4, action: 'replace', html: '<p>x</p>', startsWith: 'image' }]],
        [SPANNED, [{ paragraph: 2, action: 'delete', startsWith: 'One' }, { paragraph: 2, action: 'replace', html: '<p>x</p>', startsWith: 'One' }]],
        [SPANNED, [{ paragraph: 2, action: 'replace', startsWith: 'One' }, { paragraph: 3, action: 'move', startsWith: 'Two' }, { paragraph: 3, action: 'delete', startsWith: '  ' }]],
        [SPANNED, []], ['<p>a<p>b</p>', [{ paragraph: 1, action: 'delete', startsWith: 'a' }]],
        [CHAPTER, [{ paragraph: 2, action: 'insert_after', html: '<p>新段。</p>', startsWith: chapterParagraphs(CHAPTER)[1]?.text.slice(0, 4) ?? 'x' }]],
        // A list entry edited as itself; a mistyped anchor still lands; the edits that fit apply, the others come back.
        [CARD, [{ paragraph: 4, action: 'replace', html: '<p>身份：港务局引航员，四级。</p>', startsWith: '身份：港务局引杭员' }, { paragraph: 5, action: 'insert_after', html: '工龄：十二年。', startsWith: '• 资质' }, { paragraph: 7, action: 'delete', startsWith: '身份：港务局局长' }]],
        [CARD, [{ paragraph: 2, action: 'replace', html: '<p>适用范围：第1–25章</p>', startsWith: '适用范围' }, { paragraph: 4, action: 'replace', html: '<p>x</p>', startsWith: '完全不同的开头文字' }]]
      ] as Array<[string, ParagraphEdit[]]>)
    }
  },
  {
    module: 'diff',
    cases: {
      diff_html: run(diffHtml, [
        ['<p>Hello</p>', '<p>Hello</p>'], ['<p>Hello</p>', '<p>Hello world</p>'], ['<p>Hello world</p>', '<p>Hello</p>'],
        ['<p>foo bar</p>', '<p>foo baz</p>'], ['', '<p>Hello</p>'], ['<p>Hello</p>', ''], ['', ''], ['<p>old</p>', '<p>new</p>'],
        ['<p>a b</p>', '<p>a  b</p>'], ['<p>你好世界</p>', '<p>你好中国</p>'], ['<p>AAA BBB CCC</p>', '<p>AAA ZZZ CCC</p>'],
        ['<p>one</p><p>two</p><p>three</p>', '<p>one</p><p>TWO</p><p>three</p>'], ['<p>one</p><p>two</p>', '<p>one</p><p>new</p><p>two</p>'],
        ['<p>one</p><p>two</p><p>three</p>', '<p>one</p><p>three</p>'], [paras(false), paras(true)],
        // An <img> is marked like a word: placed (an image copied from another chapter), removed, moved (2026-10-18).
        ['<p>one</p>', '<p>one</p><p><img src="https://p/1.jpg"></p><p>two</p>'], ['<p>a <img src="x"> b</p>', '<p>a b</p>'],
        ['<p><img src="x"></p><p>t</p>', '<p>t</p><p><img src="x" alt="m"></p>'],
        [CHAPTER, CHAPTER.replace('风从巷口灌进来', '雨从檐角落下来').replace('<li>一</li>', '<li>壹</li>')],
        ['<h1>T</h1><p>x</p>', '<h2>T</h2><p>x</p>'], ['<p>a</p>', '<p>a</p><ul><li>b</li></ul>']
      ], o => normalizeDiffIds(o as string)),
      strip_diff_markup: run(stripDiffMarkup, [[DIFFED], ['<p>plain</p>'], [''], ['<p><ins>not a diff</ins> <del>either</del></p>']])
    }
  },
  {
    module: 'diff_resolution',
    cases: {
      resolve_diff_markup_in_html: run(resolveDiffMarkupInHtml, [[DIFFED, 'accept'], [DIFFED, 'reject'], ['<p>plain</p>', 'accept'],
        ['<blockquote><p><ins class="diff-addition" data-diff-id="d">q</ins></p></blockquote><p>after</p>', 'reject'],
        ['<ul><li><p><del class="diff-deletion" data-diff-id="d">gone</del></p></li></ul>', 'accept']])
    }
  },
  {
    module: 'context_ledger',
    cases: {
      hash_content: run(hashContent, [[''], ['abc'], [CHAPTER], ['😀 emoji'], ['x'.repeat(5000)]]),
      plan_ledger_turn: run(planLedger, [
        [{ entries: [] }, ['d1', 'd2'], LDOCS, 'd3'],
        [LEDGER_123, ['d1', 'd2', 'd3'], LDOCS, null],
        [LEDGER_123, ['d1', 'd3'], LDOCS, null],
        [{ entries: [L('d1', 'old', 100), L('d2', 'h2', 2000), L('d3', 'h3', 300)] }, ['d1', 'd2', 'd3'], LDOCS, null],
        [{ entries: [L('d1', 'old', 100), L('d2', 'h2', 2000), L('d3', 'h3', 300)] }, ['d1', 'd2', 'd3'], LDOCS, null, { render: true }],
        [{ entries: [L('d1', 'h1', 100), L('d2', 'old', 2000), L('d3', 'h3', 300)] }, ['d1', 'd2', 'd3'], LDOCS, null, { render: true }],
        [LEDGER_123, ['d1', 'd2', 'd3'], LDOCS, 'd2', { render: true }],
        [{ entries: [L('d1', 'old', 100, { stale: true }), L('d2', 'h2', 2000), L('d1', 'h1', 100, { text: '[update:d1]' }), L('d3', 'h3', 300)] }, ['d1', 'd2', 'd3', 'd4'], LDOCS, null, { render: true, maxStaleChars: 50 }],
        [{ entries: [L('d1', 'old', 100, { stale: true }), L('d2', 'h2', 2000), L('d1', 'h1', 100, { text: '[update:d1]' }), L('d3', 'h3', 300)] }, ['d1', 'd2', 'd3', 'd4'], LDOCS, null, { render: true }],
        [{ entries: [L('gone', 'g', 10), L('d2', 'h2', 2000)] }, ['d2', 'd2', 'd4', 'nope'], LDOCS, 'd4'],
        [{ entries: [L('d2', 'old', 2000), L('d1', 'h1', 100)] }, ['d1', 'd2'], LDOCS, null, { render: true }],
        // A pinned chapter the writer opened stays where it is, edited or not; one already gone is not kept.
        [LEDGER_123, ['d1', 'd3'], LDOCS, 'd2', { render: true, keepIds: ['d2'] }],
        [{ entries: [L('d1', 'h1', 100), L('d2', 'old', 2000), L('d3', 'h3', 300)] }, ['d1', 'd3'], LDOCS, 'd2', { render: true, keepIds: ['d2'] }],
        [{ entries: [L('gone', 'g', 10), L('d1', 'h1', 100)] }, ['d1'], LDOCS, 'gone', { keepIds: ['gone'] }],
        [{ entries: [] }, ['d1'], LDOCS, 'd2', { render: true, keepIds: ['d2'] }]
      ]),
      ledger_chapter_ids: run(ledgerChapterIds, [[{ entries: [L('a', '', 1), L('b', '', 1), L('a', '', 1)] }], [{ entries: [] }]]),
      order_admissions_by_stability: run(orderAdmissionsByStability, [
        [['a', 'b', 'c', 'd'], [{ id: 'a', updatedAt: '2026-01-05T00:00:00.000Z' }, { id: 'b', updatedAt: '2025-12-01T00:00:00Z' }, { id: 'c' }, { id: 'd', updatedAt: '2025-12-01T00:00:00Z' }], ['c', 'd', 'b', 'a'], 'c'],
        [['a', 'b', 'c', 'd'], [{ id: 'a', updatedAt: '2026-01-05T00:00:00.000Z' }, { id: 'b', updatedAt: 'not-a-date' }, { id: 'c', updatedAt: '2025-06-01' }, { id: 'd', updatedAt: '2025-12-01T00:00:00Z' }], ['c', 'd', 'b', 'a'], null],
        [['x', 'y'], [], ['y', 'x'], 'x'], [[], [], [], null]
      ])
    }
  },
  {
    module: 'protocol_choice',
    cases: {
      resolve_document_protocol: run(resolveDocumentProtocol, [['grok', undefined], ['grok', 'tools'], ['ollama', 'auto' as DocumentProtocol], ['runpod', 'markup'], ['openai', undefined], ['gemini', 'auto' as DocumentProtocol], ['weird', undefined], ['moonshot', undefined]])
    }
  },
  {
    module: 'title_sync',
    cases: {
      leading_h1_text: run(leadingH1Text, [['<h1>第一章 启程</h1><p>x</p>'], ['  <h1 class="t">A &amp; <em>B</em><del>gone</del></h1>'], ['<p>no heading</p><h1>late</h1>'], ['<h1>   </h1>'], ['<h1>unclosed'], ['']]),
      content_with_renamed_heading: run(contentWithRenamedHeading, [['<h1>Old</h1><p>x</p>', 'New & <Better>'], ['<h1>Same</h1>', 'Same'], ['<p>none</p>', 'T'], ['<h1>Old</h1>', '   '], ['<h1><ins class="diff-addition" data-diff-id="d">Old</ins></h1>', 'New'], ['<h1>Old $& $1</h1>', 'N$&'], ['\n  <h1 id="h">Old</h1><h1>Old</h1>', 'New']])
    }
  },
  {
    module: 'stream_handlers',
    cases: {
      constants: run(() => ({ NO_ACTION_RETRY_INSTRUCTION, MAX_NO_ACTION_RETRIES, ASSISTANT_PLACEHOLDER, INTERRUPTED_NOTICE, RECONNECT_FAILED_NOTICE, STEP_LIMIT_NOTE }), [[]]),
      split_streaming_response: run(splitStreamingResponse, [...RESPONSES, 'Hi <canv', 'Lead\n<canvas chapter="2">partial', '<selection_replace>half', 'a\n<edit>\n<<<<<<< SEARCH\nx'].map(r => [r] as [string])),
      build_completion_warnings: run(buildCompletionWarnings, [
        [{ canvasIssue: null, editFailedCount: 0, exhaustedNoActionRetries: false, reinsertedImages: 0 }],
        [{ canvasIssue: 'truncated', editFailedCount: 1, exhaustedNoActionRetries: false, reinsertedImages: 1, strayMarkup: 1 }],
        [{ canvasIssue: 'elided', editFailedCount: 2, exhaustedNoActionRetries: true, reinsertedImages: 2, strayMarkup: 2, selectionGone: true, unretriableFailedUpdate: true, toolCallProducedNothing: true }],
        [{ canvasIssue: null, editFailedCount: 0, exhaustedNoActionRetries: false, reinsertedImages: 0, strayMarkup: 3, unretriableFailedUpdate: true }]
      ])
    }
  },
  {
    module: 'reasoning_effort',
    cases: {
      supported_reasoning_efforts: run(supportedReasoningEfforts, [
        ['grok', 'grok-4.20-0309-reasoning'], ['grok', 'grok-4.7'], ['grok', 'grok-4.5'], ['grok', 'grok-3-mini'], ['grok', 'grok-3'],
        ['moonshot', 'kimi-k3'], ['moonshot', 'kimi-k2.6'], ['moonshot', 'kimi-k2.7-code'],
        ...['gpt-5', 'gpt-5-mini', 'gpt-5-2025-08-07', 'gpt-5-codex', 'gpt-5.1', 'gpt-5.1-codex', 'gpt-5.10', 'gpt-5.2', 'gpt-5.6-luna', 'gpt-6.1-sol', 'o3', 'o4-mini', 'gpt-4o', 'gpt-4.1'].map(m => ['openai', m] as [string, string]), ['gemini', 'gemini-2.5-flash'], ['gemini', 'gemini-1.5-pro'], ['gemini', 'gemini-3.8-flash'], ['gemini', 'gemini-3.1-pro-preview'], ['gemini', 'gemini-flash-latest'], ['gemini', 'gemini-10-flash'], ['gemini', 'gemma-4-31b-it'], ['ollama', 'x'], ['nope', 'y'],
        ...['claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-opus-5', 'claude-fable-5-1', 'claude-opus-4-8', 'claude-opus-4-7', 'claude-opus-4-6',
          'claude-sonnet-4-6', 'claude-opus-4-5-20251101', 'claude-haiku-4-5-20251001', 'claude-3-7-sonnet-latest', 'claude-3-5-sonnet'].map(m => ['anthropic', m] as [string, string])
      ]),
      resolve_reasoning_effort: run(resolveReasoningEffort, [
        ['anthropic', 'claude-sonnet-5-5', undefined], ['anthropic', 'claude-sonnet-5-5', 'default'], ['anthropic', 'claude-sonnet-5-5', 'xhigh'],
        ['anthropic', 'claude-opus-4-6', 'xhigh'], ['anthropic', 'claude-3-5-sonnet', 'high'], ['grok', 'grok-4.7', 'minimal'], ['openai', 'gpt-5', 'minimal'], ['openai', 'gpt-5.1', 'minimal'], ['openai', 'gpt-6.1-sol', 'xhigh'], ['openai', 'gpt-4o', 'low'],
        ['moonshot', 'kimi-k3', undefined], ['moonshot', 'kimi-k3', 'xhigh'], ['moonshot', 'kimi-k3', 'medium'], ['moonshot', 'kimi-k2.6', 'high']
      ]),
      moonshot_thinking: run(moonshotThinking, [['kimi-k3', 'low'], ['kimi-k3', 'xhigh'], ['kimi-k3', null], ['kimi-k2.6', null], ['kimi-k2.6', 'high'], ['kimi-k2.7-code', null], ['moonshot-v1-8k', 'low']]),
      gemini_thinking: run(geminiThinking, [['gemini-3.8-flash', null], ['gemini-3.8-flash', 'low'], ['gemini-pro-latest', 'high'], ['gemini-2.5-pro', 'medium'], ['gemini-2.5-flash', 'minimal'], ['gemini-1.5-pro', 'low'], ['gemma-4-31b-it', 'low']]),
      reasoning_budget_tokens: run(reasoningBudgetTokens, [['minimal'], ['low'], ['medium'], ['high'], ['xhigh']]),
      anthropic_thinking: run(anthropicThinking, [
        ['claude-sonnet-5-5', null, 16384], ['claude-sonnet-5-5', 'low', 16384], ['claude-opus-4-7', 'xhigh', 16384], ['claude-opus-4-6', 'minimal', 16384],
        ['claude-sonnet-4-6', 'high', 16384], ['claude-haiku-4-5-20251001', 'high', 16384], ['claude-haiku-4-5-20251001', 'high', 3000],
        ['claude-sonnet-4-5-20250929', 'low', 2000], ['claude-3-7-sonnet-latest', 'medium', 8192]
      ])
    }
  }
]

/**
 * Fixtures whose TypeScript is gone (backend_authority.md phase 4, 2026-10-17):
 * the agent loop lives only in Python now, so wc_agent / wc_text are the
 * specification for these modules. Their fixtures stay as golden files that
 * scripts/test_parity.py checks; an intended change to the Python rewrites
 * them with `python3 scripts/test_parity.py --rewrite-python-owned`.
 */
const PYTHON_OWNED = ['freshness', 'policy', 'chapters', 'invocations', 'reminders', 'run_compaction',
  'llm_context', 'pending_changes', 'image_preservation', 'polish', 'chapter_index', 'dynamic_context', 'context_selection', 'context_window', 'provider_profile', 'tool_call_stream', 'edit_hints', 'plan', 'conversation_summary', 'turn_transcripts', 'text_replace', 'web_text', 'retry_policy', 'system_prompt', 'document_tools', 'attachments']

describe('python-owned fixtures', () => {
  for (const module of PYTHON_OWNED) {
    it(`${module}: the golden file is present`, () => {
      expect(existsSync(resolve(FIXTURES, `${module}.json`))).toBe(true)
      expect(MODULES.some(m => m.module === module)).toBe(false)
    })
  }
})

describe('parity fixtures', () => {
  for (const mod of MODULES) {
    it(`${mod.module}: the committed fixtures equal the TypeScript output`, () => {
      const path = resolve(FIXTURES, `${mod.module}.json`)
      const json = JSON.stringify(mod, null, 1) + '\n'
      if (WRITE) {
        mkdirSync(FIXTURES, { recursive: true })
        writeFileSync(path, json)
        return
      }
      expect(existsSync(path), `${path} missing — run: npm run parity:fixtures`).toBe(true)
      // Both sides through JSON: an `undefined` input is null in the file.
      expect(JSON.parse(readFileSync(path, 'utf8'))).toEqual(JSON.parse(json))
    })
  }
})
