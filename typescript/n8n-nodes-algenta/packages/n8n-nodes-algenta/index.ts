// Package entry point (`main` in package.json). n8n itself discovers this package's node and
// credential through the `n8n.nodes` / `n8n.credentials` file lists in package.json, not through
// this file -- but a real `main` module lets anything else that does a plain `require()` /
// `import` of `n8n-nodes-algenta` (tooling, tests, a script auditing installed nodes) resolve to
// real, typed exports instead of hitting a missing-file error.
export { Algenta } from './nodes/Algenta/Algenta.node';
export { AlgentaApi } from './credentials/AlgentaApi.credentials';
