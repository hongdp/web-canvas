import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { exportDocument } from '../export'
import type { CanvasDocument } from '../../store/useAppStore'

describe('exportDocument', () => {
  let mockClick: ReturnType<typeof vi.fn>
  let mockAppendChild: ReturnType<typeof vi.fn>
  let mockRemoveChild: ReturnType<typeof vi.fn>
  let mockCreateObjectURL: ReturnType<typeof vi.fn>

  beforeEach(() => {
    mockClick = vi.fn()
    mockAppendChild = vi.fn()
    mockRemoveChild = vi.fn()
    mockCreateObjectURL = vi.fn().mockReturnValue('blob:test-url')

    const mockAnchor = {
      href: '',
      download: '',
      click: mockClick,
    } as unknown as HTMLAnchorElement

    vi.spyOn(document, 'createElement').mockReturnValue(mockAnchor)
    vi.spyOn(document.body, 'appendChild').mockImplementation(mockAppendChild as unknown as typeof document.body.appendChild)
    vi.spyOn(document.body, 'removeChild').mockImplementation(mockRemoveChild as unknown as typeof document.body.removeChild)
    globalThis.URL.createObjectURL = mockCreateObjectURL as unknown as typeof URL.createObjectURL
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  const dummyDoc: CanvasDocument = {
    id: 'doc1',
    title: 'Chapter 1',
    content: '<h1>Chapter 1</h1><p>Test content</p>',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString()
  }

  it('exports a single document as HTML', () => {
    exportDocument('html', false, [dummyDoc], dummyDoc, 'My Book')
    
    expect(mockCreateObjectURL).toHaveBeenCalled()
    expect(mockAppendChild).toHaveBeenCalled()
    expect(mockClick).toHaveBeenCalled()
    expect(mockRemoveChild).toHaveBeenCalled()
    
    // Check if the mock object was assigned correct download name
    const mockAnchor = vi.mocked(document.createElement).mock.results[0].value as HTMLAnchorElement
    expect(mockAnchor.download).toBe('My_Book_Chapter_1.html')
    expect(mockAnchor.href).toBe('blob:test-url')
  })

  it('exports all documents as Markdown', () => {
    const doc2: CanvasDocument = {
      id: 'doc2',
      title: 'Chapter 2',
      content: '<h1>Chapter 2</h1><p>More content</p>',
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString()
    }
    
    exportDocument('markdown', true, [dummyDoc, doc2], dummyDoc, 'My Book')
    
    expect(mockCreateObjectURL).toHaveBeenCalled()
    expect(mockClick).toHaveBeenCalled()
    
    const mockAnchor = vi.mocked(document.createElement).mock.results[0].value as HTMLAnchorElement
    expect(mockAnchor.download).toBe('My_Book.md')
  })

  it('exports a single document as TXT', () => {
    exportDocument('txt', false, [dummyDoc], dummyDoc, 'My Book Title')
    
    expect(mockCreateObjectURL).toHaveBeenCalled()
    expect(mockClick).toHaveBeenCalled()
    
    const mockAnchor = vi.mocked(document.createElement).mock.results[0].value as HTMLAnchorElement
    expect(mockAnchor.download).toBe('My_Book_Title_Chapter_1.txt')
  })
})
