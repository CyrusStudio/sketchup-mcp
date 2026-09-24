# Provenance and licensing

## Origin

This repository is a fork of **https://github.com/mhyrr/sketchup-mcp**, created
on 2026-09-24. The upstream project remains the origin of the extension design,
the tool surface and the SketchUp Ruby modelling code; this fork changes the
transport, the MCP SDK compatibility layer, the tests and the documentation.

* Fork: https://github.com/CyrusStudio/sketchup-mcp (`origin`)
* Upstream: https://github.com/mhyrr/sketchup-mcp (`upstream`)

Upstream credits [Blender MCP](https://github.com/ahujasid/blender-mcp) as the
inspiration for the structure; that credit is carried over here.

## Licensing, exactly as it stands

MIT is what upstream declares, in three places, all of which this fork preserves
unchanged:

* `pyproject.toml`: `license = { text = "MIT" }` and the
  `License :: OSI Approved :: MIT License` classifier
* `README.md`: a `## License` section reading `MIT`
* `su_mcp/extension.json`: `"license": "MIT"`

**Upstream ships no `LICENSE` file**, so there is no MIT licence text and no
copyright line naming a holder. This fork deliberately does not add one: writing
a copyright notice on the original author's behalf would be inventing a
declaration they never made.

If you need a formal licence file — for redistribution, packaging or compliance —
the right fix is to ask the upstream author to add `LICENSE` with their own
copyright line, and then mirror it here. Until then, treat the MIT declaration
above as upstream's stated intent rather than as an executed licence grant.

Nothing in this fork claims authorship of the upstream work.
