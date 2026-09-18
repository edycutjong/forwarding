# Bundled font licences

The three `.woff2` files in this directory are **not** covered by the MIT licence at the root
of this repository. Each is a third-party typeface redistributed under the
**SIL Open Font License, Version 1.1** (<https://openfontlicense.org>), which permits bundling
and redistribution with or without modification, provided the fonts are not sold on their own
and this notice travels with them.

| File | Typeface | Copyright | Licence |
|---|---|---|---|
| `inter-latin-var.woff2` | [Inter](https://github.com/rsms/inter) | © Rasmus Andersson | OFL-1.1 |
| `inter-tight-latin-var.woff2` | [Inter Tight](https://github.com/rsms/inter) | © Rasmus Andersson | OFL-1.1 |
| `jetbrains-mono-latin-var.woff2` | [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | © JetBrains s.r.o. | OFL-1.1 |

Each file is the unmodified **latin subset** of the upstream variable font, as built and served
by Google Fonts. They are vendored rather than fetched from `fonts.gstatic.com` so that the
judged page loads no third-party asset.
