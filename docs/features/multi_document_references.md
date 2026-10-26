# Feature Specification — Multi-Document & Reference Context System

> **Superseded (2026-10-06).** Reference context is now the chapters the
> writer pins ([pinned_context.md](pinned_context.md)), and the model reads
> the rest with its tools ([agentic_chat_loop.md](agentic_chat_loop.md)).
> The chapters sidebar, lazy loading and per-chapter editor contexts
> described here still stand; the attach/auto-attach mechanics do not. Kept
> as the record the Decision Log links to.

This document describes the design and behavior of the **Multi-Document (Chapters) and Reference Context System** in Web Canvas, which allows authors to work on multi-chapter projects while sharing context across documents.

---

## 1. Architectural Overview

The system expands the single-document canvas into a multi-document layout, allowing users to toggle between different chapters on the fly and feed selected chapters into the LLM context.

```
┌─────────────────────────────────────────────────────────────┐
│                       Web Canvas Shell                      │
├─────────────────┬─────────────────┬─────────────────────────┤
│                 │                 │                         │
│  Chapters       │  Assistant      │  Document Canvas        │
│  Sidebar        │  Chat Panel     │  (Active Chapter)       │
│  (Vertical)     │                 │                         │
│                 │  ┌───────────┐  │  • Main Rich Text       │
│  • Chapter 1    │  │Chat Input │  │  • Editable Title       │
│  • Chapter 2    │  └───────────┘  │  • Auto-saves           │
│  • Chapter 3    │  ┌───────────┐  │                         │
│                 │  │Reference  │  │                         │
│                 │  │Tag Selector  │                         │
│                 │  └───────────┘  │                         │
└─────────────────┴─────────────────┴─────────────────────────┘
```

---

## 2. Multi-Document Data Model

Documents are represented in a unified list inside the global state:

```typescript
export interface CanvasDocument {
  id: string
  title: string
  content: string       // Rich HTML markup
  createdAt: string
  updatedAt: string
}
```

The Zustand state store ([useAppStore.ts](/src/store/useAppStore.ts)) keeps track of:
- `documents`: Array of all `CanvasDocument` items.
- `activeDocumentId`: The ID of the document currently loaded in the TipTap editor.
- `selectedReferenceIds`: Array of document IDs manually attached by the user as references.
- `isSidebarOpen`: Boolean flag for the collapsible sidebar panel.

---

## 3. Reference Context Integration

When communicating with the Gemini API, the app compiles three distinct layers of context to provide full project awareness:

### 3.1 Chapter Outline (Metadata Context)
Gemini is always provided with the complete chapter list and hierarchy:
```markdown
CHAPTER OUTLINE (OVERVIEW OF ALL WRITTEN CHAPTERS):
- Chapter 1: Introduction
- Chapter 2: Setup Guide (Active / Editing Target)
- Chapter 3: Implementation Detail
```

### 3.2 Active Document (Target Context)
The content of the active chapter is sent as the primary editing context. Gemini is instructed that this is the **only** document it is allowed to modify.

### 3.3 Reference Documents (Read-Only Context)
The contents of any attached reference chapters are appended to the context:
```markdown
REFERENCED DOCUMENT CONTEXTS (Read-only, do not modify these but use them for details/consistency):
REFERENCE DOCUMENT "Chapter 1: Introduction" (READ-ONLY):
"""
<h1>Getting Started</h1>
<p>This is the content of Chapter 1...</p>
"""
```

---

## 4. Context Selection Mechanisms

> **Superseded:** context selection has been upgraded to a layered system —
> always-on chapter index, score-based auto-selection with pin/block manual
> control, agentic `<lookup>` fetching, and a whole-book mode. See
> [smart_context_selection.md](smart_context_selection.md) for the current
> design. Summary of what replaced the mechanisms below:
>
> - The per-doc `selectedReferenceIds` (cleared after every send) became
>   **sticky `pinnedReferenceIds`** plus **`blockedReferenceIds`**; tags now
>   cycle neutral → pinned → blocked and show live auto-selection previews.
> - Title-mention scanning still exists but is one signal inside the
>   deterministic scorer (`src/utils/contextSelection.ts`).
> - `[Attached Context: …]` labels remain, now marking `(auto)` attachments.
