# Provider network payload seam (STEP-13-2 fixture scope)

`provider_network_payload.install_soft_controls(provider_id, worktree)` composes
Qwen's `.qwen/settings.json` after a managed or plain worktree is installed.
It preserves other settings and refuses malformed settings. Codex and Claude
use launch arguments and need no settings mutation.

`controller._run_provider(..., requested_network_profile="soft_guardrail_network")`
passes the explicit string to `resolve_launch` immediately before
`spawn_provider`. The latter returns the exact argv and facts containing
requested/effective profile, reason, enforcement sources, suppressed native
tools, and uncontrolled surfaces. A missing, malformed, redirected, or changed
Qwen setting, or an unverified provider version, reports `uncontrolled_network`;
the controller refuses that spawn. Invalid explicit profiles also prove no
provider started. `None` preserves the existing launch behavior. Resume and
correction attempts pass through the same controller call. This fixture seam
does not add a field to a shared invocation or task-card schema.

For the installed Codex 0.156.1, Claude Code 2.1.278, and Qwen Code 0.21.10:

| Provider | Verified native control |
| --- | --- |
| Codex | Top-level `web_search="disabled"` via `-c` in the actual exec argv. |
| Claude Code | `--disallowedTools WebSearch WebFetch` in the actual argv. |
| Qwen Code | Worktree `tools.webSearch.enabled=false`, `tools.disabled` and `permissions.deny` for `web_search` and `web_fetch`, read again at spawn; 0.21.10 `--exclude-tools web_search,web_fetch` in the actual argv provides the effective whole-tool deny. |

Qwen may ignore project settings in an untrusted folder, and
`ENABLE_WEB_SEARCH=true` overrides `tools.webSearch.enabled`. Its 0.21.10 CLI
merges `--exclude-tools` into the deny rules after loading settings, and its
permission manager removes whole-tool denies from registration. The launch
claim therefore cites the argv control; raw worktree settings are still
required and checked as installed payload, but are not treated alone as
effective enforcement. The installer checks reparse points and physical
worktree containment before any settings read or write.

The controls suppress those provider-native tools. Shell commands, other
provider tools, and user-configured MCP/plugin/extension tools can still reach
the network. No hardened sandbox or Atlas-only isolation is claimed. An
`atlas_memory_only` request downgrades to soft when those native controls are
verified because independent unrelated-destination blocking is unmeasured.
A `restricted_local` request also reports only soft from this payload seam;
the controls add no optional Atlas task-path access, but the service-boundary
forbidden-call proof is pending. Lane 1 must join its resolved profile and
STEP-15 facade; STEP-17/18 joined checks remain pending.

Provider references: [Codex configuration](https://developers.openai.com/codex/config-reference),
[Claude CLI](https://code.claude.com/docs/en/cli-reference),
[Qwen settings](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/settings/),
[Qwen web search](https://qwenlm.github.io/qwen-code-docs/en/developers/tools/web-search/).
