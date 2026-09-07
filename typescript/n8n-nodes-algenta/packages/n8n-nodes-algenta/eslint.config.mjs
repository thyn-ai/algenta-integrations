import { configWithoutCloudSupport } from '@n8n/node-cli/eslint';

// This node depends on the official `@modelcontextprotocol/sdk` to speak MCP correctly (the same
// package n8n's own bundled `n8n-nodes-langchain.mcpClient` node uses) -- n8n Cloud's community
// node policy bans runtime dependencies entirely, which this package cannot satisfy without
// hand-rolling MCP's session/handshake/SSE framing itself. This is a fully real, tested,
// installable community node for SELF-HOSTED n8n (this program's actual target audience); it is
// just not eligible for n8n's own Cloud verification program. See the README's "n8n Cloud
// eligibility" section, which documents this and the two overrides below in full.
export default [
	...configWithoutCloudSupport,
	{
		files: ['package.json'],
		rules: {
			// The one runtime dependency this package has is exactly the tradeoff described above,
			// not an oversight -- see the README.
			'@n8n/community-nodes/no-runtime-dependencies': 'off',
			// n8n's own lint hard-requires this field to read exactly "MIT" -- this package
			// deliberately declares Apache-2.0 instead, for org-wide license consistency with the
			// rest of this repository (owner decision, made with the tradeoff understood: this
			// likely blocks listing in n8n's official community-node registry). See the README's
			// "License" section.
			'n8n-nodes-base/community-package-json-license-not-default': 'off',
		},
	},
	{
		files: ['src/mcp-client.ts', 'test/test-support/stub-server.ts'],
		rules: {
			// `import-x`'s resolver doesn't follow @modelcontextprotocol/sdk's `exports` map in this
			// setup; `tsc` (see `pnpm build`) resolves these subpaths correctly, and the test suite
			// exercises them over a real socket -- see the README's "n8n Cloud eligibility" section.
			'import-x/no-unresolved': 'off',
		},
	},
	{
		files: ['nodes/Algenta/Algenta.node.ts'],
		rules: {
			// This description's "contracts/integration-tool-contract.json" is a real, correct file
			// path, not prose that should read "...contract.JSON" -- the autofix for this rule would
			// corrupt a real filename reference.
			'n8n-nodes-base/node-param-description-miscased-json': 'off',
		},
	},
];
