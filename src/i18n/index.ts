import { useAppStore } from '../store/useAppStore'
import { en } from './en'
import { zh } from './zh'

export type Language = 'en' | 'zh'

export type TranslationDictionary = typeof en

const dictionaries: Record<Language, TranslationDictionary> = {
  en,
  zh,
}

/** The dictionary for the current language, outside React (a hook's callbacks). */
export const currentTranslation = (): TranslationDictionary =>
  dictionaries[useAppStore.getState().language] || dictionaries.en

export const useTranslation = () => {
  const language = useAppStore((state) => state.language)
  const t = dictionaries[language] || dictionaries.en

  return { t, language }
}
