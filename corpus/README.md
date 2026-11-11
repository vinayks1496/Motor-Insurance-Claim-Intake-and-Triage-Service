# Policy corpus and source handling

The project references three insurer-hosted public documents listed in `sources.json`: the New India Assurance private-car base package policy wording, the private-car use section of the India Motor Tariff 2002, and New India Assurance motor claim procedural guidance. The one-shot indexer fetches the full PDFs into a Docker volume when reachable. `pages: "all"` indexes all pages for the policy and claim guide; the tariff entry scopes the import to the listed private-car section pages.

The Markdown files in this directory are compact offline excerpts and paraphrases for use when source downloads fail. They are not complete policy wording. Each passage and citation retains the source name and official URL. The index manifest distinguishes `official_pdf` from `offline_excerpt`; the handler should review the actual issued schedule, endorsements, and applicable policy documents before acting.

Sources are provided for educational project use. Verify the current wording and version with the insurer before using it in any real workflow.
