# 510k-browser

Browse FDA 510(k) clearances, read their summary documents, and explore the graph of
devices and the predicates they claimed substantial equivalence to.

The site is fully static (GitHub Pages). A scheduled GitHub Action rebuilds the data.
See [PLAN.md](PLAN.md) for the full design and milestones.

> **Informational only.** This project is not affiliated with the FDA. Predicate links
> are extracted automatically and may be wrong; always verify against the FDA record.

## Development

Requires [uv](https://github.com/astral-sh/uv).

```sh
uv sync                  # create the venv and install dependencies
uv run fda510k --help    # pipeline CLI
uv run pytest            # tests
uv run ruff format       # format
uv run ruff check        # lint
uv run ty check          # type check
```

## Pipeline

```sh
uv run fda510k ingest            # openFDA 510(k) + classification bulk data -> build/
uv run fda510k graph             # seed/ + state/manual_overrides.csv -> build/graph_edges.parquet
uv run fda510k build-site-data   # -> build/site-data/ (static JSON for the frontend)
uv run fda510k all               # all three, in order
```

`uv run fda510k probe` checks whether the current machine can fetch from
accessdata.fda.gov: 7 requests, 30 s apart, only between 03:00 and 09:00 UTC (FDA's
robots.txt). Outside that window it sends nothing and exits with code 3; pass
`--wait-hours N` to wait for the window instead. On GitHub, run the
**Probe accessdata.fda.gov** workflow by hand. Results land in `build/probe/`.

`build/` is never committed. The committed inputs are:

- `seed/`: predicate edges imported once from
  [wcedmisten/fda-510k-analysis](https://github.com/wcedmisten/fda-510k-analysis)
  (see [seed/SOURCE.md](seed/SOURCE.md)). Regenerate with `fda510k import-seed --from <clone>`.
- `state/manual_overrides.csv`: hand corrections (`device,predicate,action,reason,author`,
  where `action` is `add` or `remove`). Applied last.

## Frontend

A static Preact + Vite app in `site/`. It reads the JSON from `build/site-data/`
(override with `SITE_DATA_DIR`), so run the pipeline first.

```sh
uv run fda510k all
cd site
npm ci
npm run dev        # http://localhost:5173
npm test           # vitest
npm run lint       # biome
npm run typecheck
npm run build      # -> site/dist (copy build/site-data to dist/data to deploy)
```

## Deployment

`.github/workflows/site.yml` builds the data and the site on every PR and push to
`main`. It deploys to GitHub Pages from `main` once you:

1. set Pages to deploy from **GitHub Actions** (Settings → Pages), and
2. add the repository variable `PAGES_ENABLED=true`.

On free GitHub plans, Pages requires a public repository.

## License

Code: [MIT](LICENSE).
