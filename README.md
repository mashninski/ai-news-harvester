# ai-news-harvester

Збіральнік навінаў пра штучны інтэлект для будучага раздзелу навінаў на [mashninski.com](https://mashninski.com): чытае стужкі OpenAI, Google DeepMind, Hugging Face, TechCrunch, VentureBeat, MIT Technology Review і Simon Willison, а таксама sitemap Anthropic, прыводзіць запісы да агульнага выгляду, адкідае ўжо бачанае і склейвае адну навіну з розных крыніцаў у адну.

Другі крок — пайплайн: адбор навінаў (Claude Haiku), пераказ па-беларуску (Claude Sonnet), канвэртацыя ў тарашкевіцу праз [taraskevizer](https://github.com/GooseOb/taraskevizer) з ахоўнымі маркерамі, праверка на русізмы і калькі і выпраўленьне пазначаных сказаў. Усе выклікі мадэляў ідуць праз Batch API. На выхадзе — чарнавікі картак для ручнога рэўю.
