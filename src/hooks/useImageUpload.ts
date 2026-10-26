import { useState, useRef, useCallback } from 'react'
import { convertBlobUrlToDataUrl, convertGifToJpegIfNeeded } from '../utils/text'

export function useImageUpload() {
  const [uploadedImages, setUploadedImages] = useState<string[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const processFiles = useCallback((files: File[]) => {
    const imageFiles = files.filter(file => file.type.startsWith('image/'))
    if (imageFiles.length === 0) return

    // Filter out files exceeding the size limit (invalid images) first.
    // GIFs can be up to 15MB, other formats up to 2MB.
    const validSizeFiles = imageFiles.filter(file => {
      const isGif = file.type === 'image/gif'
      const limit = isGif ? 15 * 1024 * 1024 : 2 * 1024 * 1024
      if (file.size > limit) {
        alert(`Image "${file.name}" exceeds the ${isGif ? '15MB' : '2MB'} size limit.`)
        return false
      }
      return true
    })
    if (validSizeFiles.length === 0) return

    setUploadedImages(prev => {
      // Check maximum attachment limit using only valid images
      if (prev.length + validSizeFiles.length > 3) {
        alert('You can attach a maximum of 3 images.')
        return prev
      }

      // Process valid files asynchronously, but we handle the async resolution 
      // by appending to state later. To avoid race conditions with multiple rapid
      // uploads, we rely on the functional state update inside the reader onload.
      // However, we already checked the capacity based on current + new valid files.
      // We will let the async callbacks do individual functional updates.
      return prev
    })

    // To be safe with rapid sequential drops, the capacity check inside the reader is still needed
    validSizeFiles.forEach(file => {
      const reader = new FileReader()
      reader.onload = async (event) => {
        if (event.target?.result) {
          const rawDataUrl = event.target.result as string
          try {
            const processedDataUrl = await convertGifToJpegIfNeeded(rawDataUrl)
            
            // Post-check size to ensure it fits in 2MB limit after conversion
            const approxSize = processedDataUrl.length * 0.75
            if (approxSize > 2 * 1024 * 1024) {
              alert(`Image "${file.name}" is too large (exceeds 2MB) after conversion.`)
              return
            }

            setUploadedImages(prev => {
              if (prev.length >= 3) {
                alert('You can attach a maximum of 3 images.')
                return prev
              }
              return [...prev, processedDataUrl]
            })
          } catch (err) {
            console.error('Failed to process image file:', err)
          }
        }
      }
      reader.readAsDataURL(file)
    })
  }, [])

  const handleImageUpload = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files) return
    const files = Array.from(e.target.files)
    processFiles(files)
    e.target.value = ''
  }, [processFiles])

  const handlePaste = useCallback(async (e: React.ClipboardEvent<HTMLDivElement>) => {
    const files: File[] = []
    
    // Check e.clipboardData.files first (preferred by some mobile browsers)
    if (e.clipboardData.files && e.clipboardData.files.length > 0) {
      for (let i = 0; i < e.clipboardData.files.length; i++) {
        const file = e.clipboardData.files[i]
        if (file && file.type.startsWith('image/')) {
          files.push(file)
        }
      }
    }
    
    // Fallback to e.clipboardData.items
    if (files.length === 0 && e.clipboardData.items) {
      const items = e.clipboardData.items
      for (let i = 0; i < items.length; i++) {
        if (items[i].type.startsWith('image/')) {
          const file = items[i].getAsFile()
          if (file) {
            files.push(file)
          }
        }
      }
    }

    if (files.length > 0) {
      e.preventDefault() // Prevent pasting image binary raw text
      processFiles(files)
      return
    }

    // Fallback for browsers that trigger paste event but do not expose inline clipboardData files
    try {
      if (navigator.clipboard && navigator.clipboard.read) {
        const clipboardItems = await navigator.clipboard.read()
        const clipFiles: File[] = []
        for (const item of clipboardItems) {
          // Direct image types
          for (const type of item.types) {
            if (type.startsWith('image/')) {
              try {
                const blob = await item.getType(type)
                const file = new File([blob], `pasted-image.${type.split('/')[1] || 'png'}`, { type })
                clipFiles.push(file)
              } catch (getTypeErr) {
                console.warn(`Failed to read type ${type} from inline paste fallback:`, getTypeErr)
              }
            }
          }
          // Fallback html image tags
          if (clipFiles.length === 0 && item.types.includes('text/html')) {
            try {
              const blob = await item.getType('text/html')
              const htmlText = await blob.text()
              const parser = new DOMParser()
              const doc = parser.parseFromString(htmlText, 'text/html')
              const imgs = Array.from(doc.getElementsByTagName('img'))
              for (const img of imgs) {
                const src = img.src
                if (src) {
                  if (src.startsWith('data:')) {
                    const res = await fetch(src)
                    const resBlob = await res.blob()
                    const file = new File([resBlob], 'pasted-image.png', { type: resBlob.type })
                    clipFiles.push(file)
                  } else if (src.startsWith('blob:')) {
                    const dataUrl = await convertBlobUrlToDataUrl(src)
                    const res = await fetch(dataUrl)
                    const resBlob = await res.blob()
                    const file = new File([resBlob], 'pasted-image.png', { type: resBlob.type })
                    clipFiles.push(file)
                  } else if (src.startsWith('http')) {
                    try {
                      const res = await fetch(src, { mode: 'cors' })
                      const resBlob = await res.blob()
                      const file = new File([resBlob], 'pasted-image.png', { type: resBlob.type })
                      clipFiles.push(file)
                    } catch (fetchErr) {
                      console.warn('Failed to fetch external inline image:', fetchErr)
                    }
                  }
                }
              }
            } catch (htmlErr) {
              console.warn('Failed to parse HTML from inline paste fallback:', htmlErr)
            }
          }
        }
        if (clipFiles.length > 0) {
          e.preventDefault()
          processFiles(clipFiles)
        }
      }
    } catch (err) {
      console.warn('Fallback Clipboard API read failed:', err)
    }
  }, [processFiles])

  const handlePasteFromClipboard = useCallback(async () => {
    // Check permission if query is supported
    if (navigator.permissions && navigator.permissions.query) {
      try {
        const permission = await navigator.permissions.query({
          // 'clipboard-read' is not in the TS DOM PermissionName union yet
          name: 'clipboard-read' as unknown as PermissionName
        })
        if (permission.state === 'denied') {
          alert('Clipboard read permission is denied. Please enable clipboard access for this site in your browser settings.')
          return
        }
      } catch {
        // clipboard-read permission query might not be supported, ignore
      }
    }

    try {
      if (!navigator.clipboard || !navigator.clipboard.read) {
        alert('Your browser does not support the clipboard reading API.')
        return
      }
      
      const clipboardItems = await navigator.clipboard.read()
      const files: File[] = []
      
      for (const item of clipboardItems) {
        // Direct image types
        for (const type of item.types) {
          if (type.startsWith('image/')) {
            try {
              const blob = await item.getType(type)
              const file = new File([blob], `clipboard-image.${type.split('/')[1] || 'png'}`, { type })
              files.push(file)
            } catch (getTypeErr) {
              console.warn(`Failed to read type ${type} from clipboard:`, getTypeErr)
            }
          }
        }

        // Fallback: HTML content image tags
        if (files.length === 0 && item.types.includes('text/html')) {
          try {
            const blob = await item.getType('text/html')
            const htmlText = await blob.text()
            const parser = new DOMParser()
            const doc = parser.parseFromString(htmlText, 'text/html')
            const imgs = Array.from(doc.getElementsByTagName('img'))
            for (const img of imgs) {
              const src = img.src
              if (src) {
                if (src.startsWith('data:')) {
                  const res = await fetch(src)
                  const resBlob = await res.blob()
                  const file = new File([resBlob], 'clipboard-image.png', { type: resBlob.type })
                  files.push(file)
                } else if (src.startsWith('blob:')) {
                  const dataUrl = await convertBlobUrlToDataUrl(src)
                  const res = await fetch(dataUrl)
                  const resBlob = await res.blob()
                  const file = new File([resBlob], 'clipboard-image.png', { type: resBlob.type })
                  files.push(file)
                } else if (src.startsWith('http')) {
                  try {
                    const res = await fetch(src, { mode: 'cors' })
                    const resBlob = await res.blob()
                    const file = new File([resBlob], 'clipboard-image.png', { type: resBlob.type })
                    files.push(file)
                  } catch (fetchErr) {
                    console.warn('Failed to fetch external clipboard image:', fetchErr)
                  }
                }
              }
            }
          } catch (htmlErr) {
            console.warn('Failed to parse HTML from clipboard:', htmlErr)
          }
        }
      }
      
      if (files.length > 0) {
        processFiles(files)
      } else {
        alert('No image found in clipboard. Please make sure you have copied an image first.')
      }
    } catch (err) {
      console.error('Failed to read clipboard:', err)
      alert('Could not read clipboard. Please ensure you copy an image and grant clipboard permissions.')
    }
  }, [processFiles])

  return {
    uploadedImages,
    setUploadedImages,
    fileInputRef,
    processFiles,
    handleImageUpload,
    handlePaste,
    handlePasteFromClipboard
  }
}
