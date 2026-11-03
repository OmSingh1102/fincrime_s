"""The engine under the console's file book (the reconciliation log): the
console draws its documents and reads names and addresses with these
modules.

Package map:
  loader / parse     the CSV becomes case records (every field parsed, nothing judged)
  names / addresses  rule tables for names and addresses (the AI Agent's checks use them)
  similarity         folding, Jaro-Winkler, phonetic keys
  documents          synthetic uploads: a deterministic defect plan and SVG rendering
  llm                the Straive LLM Foundry gateway the AI Agent calls Claude through
  data/              the reconciliation log (and the variance reference that came with it)
"""
