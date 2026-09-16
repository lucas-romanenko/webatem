# Contributing

Pull requests are welcome, and one kind especially.

## The contribution this project needs most

WebATEM has only ever run against ATEM 1 M/E Constellation HD switchers. If
you have any other ATEM, you can do something the author cannot.

- **Tell me what happened**, working or broken, through the
  [switcher report](../../issues/new?template=switcher-report.yml). This is
  useful on its own and needs no code.
- **Fix what you find.** A patch for a model I do not own is welcome even
  though I cannot test it.

I will take a fix for hardware I do not have on your word plus a green test
suite. In exchange, tell me in the pull request what you ran it against and
what you saw. That is the whole verification either of us gets, so it has to
be written down.

## What is likely to be merged

- Support for switcher models, video modes and features that do not work today.
- Bugs, with a test that fails before the change and passes after where that
  is possible.
- Documentation that was wrong or missing.
- Accessibility and phone or tablet layout fixes.

## What is likely to be declined, and why it is nothing personal

- **Anything specific to one organisation.** This app is deliberately generic.
  A host that needs its own behaviour gets it through the hooks class
  (`atem_control/hooks.py`), not through a branch in the app.
- **Wire-level protocol changes.** Those belong in
  [bmdwire](https://github.com/lucas-romanenko/bmdwire), where the protocol
  lives. A new ATEM or HyperDeck command is a bmdwire pull request; WebATEM
  picks it up on its next release.
- **A login screen.** The absence of one is the documented threat model, not
  an oversight. See [SECURITY.md](SECURITY.md). Put an authenticating proxy
  in front.
- **Large rewrites arriving unannounced.** Open an issue first so neither of
  us wastes an evening.

## Running it

```bash
pip install -e ".[test,desktop]"     # Python 3.10 or newer
pytest -q                            # no hardware needed
```

The suite runs without a switcher. Continuous integration runs it on every
push and pull request, so a red run is a real signal.

The stylesheet is compiled: `npm run build:css` after adding a utility class,
or it silently does not exist.

## Licensing

WebATEM is MIT. By opening a pull request you agree your contribution is
released under the same licence. The ATEM protocol library it installs,
atemwire (part of the bmdwire distribution), is LGPL-3.0-only and lives in
the other repository, which matters if your change belongs there instead.
