// TipTap extension definitions used by the Editor component.
// Kept in a separate module (not Editor.tsx) so the component file only
// exports components, which is required for Vite fast refresh.
import { Mark, mergeAttributes, Extension, Node } from '@tiptap/react'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'

declare module '@tiptap/core' {
  interface Commands<ReturnType> {
    indent: {
      indent: () => ReturnType
    }
    outdent: {
      outdent: () => ReturnType
    }
  }
}

export const IndentExtension = Extension.create({
  name: 'indent',

  addOptions() {
    return {
      types: ['paragraph', 'heading', 'blockquote', 'listItem'],
      indentSize: 24, // in px
      maxIndent: 8,
    }
  },

  addGlobalAttributes() {
    return [
      {
        types: this.options.types,
        attributes: {
          marginLeft: {
            default: null,
            parseHTML: element => element.style.marginLeft || null,
            renderHTML: attributes => {
              if (!attributes.marginLeft) {
                return {}
              }
              return { style: `margin-left: ${attributes.marginLeft}` }
            },
          },
        },
      },
    ]
  },

  addCommands() {
    return {
      indent: () => ({ tr, state, dispatch }) => {
        let hasChanged = false
        const { selection } = state
        tr.doc.nodesBetween(selection.from, selection.to, (node, pos) => {
          if (this.options.types.includes(node.type.name)) {
            const currentIndent = node.attrs.marginLeft ? parseInt(String(node.attrs.marginLeft).replace('px', '')) || 0 : 0
            const nextIndent = currentIndent + this.options.indentSize
            if (nextIndent <= this.options.maxIndent * this.options.indentSize) {
              tr.setNodeMarkup(pos, undefined, {
                ...node.attrs,
                marginLeft: `${nextIndent}px`,
              })
              hasChanged = true
            }
          }
        })
        if (hasChanged && dispatch) {
          dispatch(tr)
        }
        return hasChanged
      },
      outdent: () => ({ tr, state, dispatch }) => {
        let hasChanged = false
        const { selection } = state
        tr.doc.nodesBetween(selection.from, selection.to, (node, pos) => {
          if (this.options.types.includes(node.type.name)) {
            const currentIndent = node.attrs.marginLeft ? parseInt(String(node.attrs.marginLeft).replace('px', '')) || 0 : 0
            if (currentIndent > 0) {
              const nextIndent = Math.max(0, currentIndent - this.options.indentSize)
              tr.setNodeMarkup(pos, undefined, {
                ...node.attrs,
                marginLeft: nextIndent > 0 ? `${nextIndent}px` : null,
              })
              hasChanged = true
            }
          }
        })
        if (hasChanged && dispatch) {
          dispatch(tr)
        }
        return hasChanged
      },
    }
  },
})

// Custom TipTap Mark Extension for Proposed Additions (<ins>)
export const DiffAddition = Mark.create({
  name: 'diffAddition',
  addAttributes() {
    return {
      'data-diff-id': {
        default: null,
        parseHTML: element => element.getAttribute('data-diff-id'),
        renderHTML: attributes => {
          if (!attributes['data-diff-id']) return {}
          return { 'data-diff-id': attributes['data-diff-id'] }
        }
      }
    }
  },
  parseHTML() {
    return [{ tag: 'ins' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['ins', mergeAttributes(HTMLAttributes, { class: 'diff-addition' }), 0]
  }
})

// Custom TipTap Mark Extension for Proposed Deletions (<del>)
export const DiffDeletion = Mark.create({
  name: 'diffDeletion',
  addAttributes() {
    return {
      'data-diff-id': {
        default: null,
        parseHTML: element => element.getAttribute('data-diff-id'),
        renderHTML: attributes => {
          if (!attributes['data-diff-id']) return {}
          return { 'data-diff-id': attributes['data-diff-id'] }
        }
      }
    }
  },
  parseHTML() {
    return [{ tag: 'del' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['del', mergeAttributes(HTMLAttributes, { class: 'diff-deletion' }), 0]
  }
})

// Custom TipTap Node Extension for Images
export const CustomImage = Node.create({
  name: 'image',
  group: 'inline',
  inline: true,
  selectable: true,
  draggable: true,
  addAttributes() {
    return {
      src: { default: null },
      alt: { default: null },
      title: { default: null },
      width: { default: null },
      height: { default: null },
      style: { default: null },
    }
  },
  parseHTML() {
    return [{ tag: 'img[src]' }]
  },
  renderHTML({ HTMLAttributes }) {
    return ['img', mergeAttributes(HTMLAttributes)]
  },
})

// Custom TipTap Extension to keep visual selection highlight when editor is blurred (e.g. chat input focused)
export const BlurredSelection = Extension.create({
  name: 'blurredSelection',

  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('blurredSelection'),
        props: {
          // Arrow function so `this` stays bound to the extension (avoids a
          // `this` alias; the plugin's own `this` is not needed here).
          decorations: (state) => {
            const editor = this.editor
            if (editor && !editor.isFocused) {
              const { from, to, empty } = state.selection
              if (!empty) {
                return DecorationSet.create(state.doc, [
                  Decoration.inline(from, to, {
                    class: 'blurred-selection-highlight'
                  })
                ])
              }
            }
            return DecorationSet.empty
          }
        }
      })
    ]
  }
})

/**
 * A paragraph holding at least this many hard breaks is a pasted "wall of
 * lines", not deliberate soft breaks. Mirrors MIN_BRS_TO_SPLIT in
 * utils/convert so both layers agree on what counts as a wall.
 */
const MIN_BREAKS_TO_SPLIT = 2

/**
 * Split paragraphs that accumulate hard breaks into real paragraphs.
 *
 * Problem: normalizing `<br>` walls in transformPastedHTML only covers real
 *   clipboard paste events. On Android, pasting through the keyboard/IME
 *   arrives as `beforeinput` with inputType "insertText" and the newlines
 *   inline in `data` — no paste event, no clipboardData — so ProseMirror
 *   turned 150+ newlines into hard breaks inside ONE paragraph and the HTML
 *   normalizer never ran. (Measured on-device: inputType "insertText",
 *   data with 151 newlines, clipboardData null.)
 * Fix: do it at the document level instead of the input level, where every
 *   entry path converges — clipboard paste, IME commit, drag & drop and
 *   programmatic inserts all end up as a transaction.
 *
 * Idempotent: after splitting, no paragraph holds enough breaks to qualify,
 * so the appended transaction never re-triggers itself. A single break is
 * left alone — there it is usually a deliberate soft break.
 */
export const ParagraphsFromLineBreaks = Extension.create({
  name: 'paragraphsFromLineBreaks',

  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('paragraphsFromLineBreaks'),
        appendTransaction: (transactions, _oldState, newState) => {
          if (!transactions.some(tr => tr.docChanged)) return null

          const paragraph = newState.schema.nodes.paragraph
          const hardBreak = newState.schema.nodes.hardBreak
          if (!paragraph || !hardBreak) return null

          const targets: { pos: number; node: typeof newState.doc }[] = []
          newState.doc.descendants((node, pos) => {
            if (node.type !== paragraph) return true
            let breaks = 0
            node.forEach(child => { if (child.type === hardBreak) breaks++ })
            if (breaks >= MIN_BREAKS_TO_SPLIT) targets.push({ pos, node })
            return false // paragraphs hold inline content only
          })
          if (targets.length === 0) return null

          const tr = newState.tr
          // Rewrite from the end so earlier positions stay valid.
          for (let i = targets.length - 1; i >= 0; i--) {
            const { pos, node } = targets[i]
            const paragraphs: typeof node[] = []
            let run: typeof node[] = []
            const flush = () => {
              // Empty runs (consecutive breaks) never become empty paragraphs.
              if (run.length > 0) {
                paragraphs.push(paragraph.create(node.attrs, run))
                run = []
              }
            }
            node.forEach(child => {
              if (child.type === hardBreak) flush()
              else run.push(child)
            })
            flush()
            if (paragraphs.length < 2) continue
            tr.replaceWith(pos, pos + node.nodeSize, paragraphs)
          }

          return tr.docChanged ? tr : null
        }
      })
    ]
  }
})
