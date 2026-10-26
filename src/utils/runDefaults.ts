/**
 * The defaults the Settings form shows for a provider's run settings. The run
 * itself is the server's (scripts/wc_text/policy.py, scripts/wc_agent/polish.py
 * hold the values it applies); these are kept equal to them for the form's
 * placeholders and hints.
 */

/** Steps a turn may take before it must answer (0 in Settings means unlimited). */
export function defaultMaxSteps(provider: string): number {
  return provider === 'ollama' || provider === 'runpod' ? 10 : 6
}

/** A write's result goes back to the model like any tool result: on for every provider. */
export function defaultContinueAfterWrites(provider: string): boolean {
  void provider
  return true
}

/** The model the Polish button and polish_chapter use when none is set. */
export function defaultPolishModel(provider: string, chatModel: string): string {
  return provider === 'grok' ? 'grok-4.20-0309-reasoning' : chatModel
}

/**
 * Whether polishing is offered: only with a model other than the chat model
 * (the server's wc_agent/polish.py polish_offered). The same model re-writing
 * its own prose in small chunks, without their context, adds nothing.
 */
export function polishOffered(provider: string, config: { model?: string; polishModel?: string } | undefined): boolean {
  const chat = (config?.model ?? '').trim()
  const polish = (config?.polishModel ?? '').trim() || defaultPolishModel(provider, chat)
  return polish.trim() !== chat
}
