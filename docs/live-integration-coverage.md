# Live Integration Coverage

The live suite (`tests/live`) drives a running server-mode stack from `deploy/live`, and personal-mode SPARK on the same host, over HTTP and in a headless Chromium. Every test there is marked `integration` and skipped unless `SPARK_LIVE_STACK` names a stack, so CI and the wave gate deselect it; `deploy/live/RUNBOOK.md` says how to run it.

This page is generated from the registry in `tests/live/coverage.py` with `python -m tests.live.coverage`. The default suite checks that every live test is registered and that this page matches the registry.

## Suites

- `rc1`: test/0.3.0-live-e2e: the A18 rc1 checklist (items 0 to 11).
- `conduit`: test/0.3.0-live-e2e-conduit: connections, credits and conduit (scopes 2 to 6).
- `live-provider`: test/0.3.0-live-e2e: one live chat through a real service (item 6).
- `projects-charts`: test/0.3.0-projects-charts-verification: projects, charts, the panel and the built-in skills (scopes 1 to 5).

## rc1

| Case | Scope | Rulings | Mode | Proves | Test |
|---|---|---|---|---|---|
| RC1-00 | item 0 | A18 | server | The first administrator from bootstrap and two invited users exist. | `test_rc1.py::test_stack_has_the_created_administrator_and_two_invited_users` |
| RC1-11a | item 11 | A18 | server | An invitation is mailed over STARTTLS and completed from its link. | `test_rc1.py::test_invitation_is_mailed_and_completed_from_the_link` |
| RC1-11b | item 11 | A18 | server | A password reset is mailed, ends the old session and changes the password. | `test_rc1.py::test_password_reset_is_mailed_and_completed_from_the_link` |
| RC1-01 | item 1 | A18 | server | Single sign-on against the OIDC provider behind the throwaway CA. | `test_rc1.py::test_single_sign_on_against_the_oidc_provider` |
| RC1-02 | item 2 | A18 | server | A tool category a User may not have is neither offered nor run. | `test_rc1.py::test_a_tool_category_a_user_may_not_have_is_refused` |
| RC1-08 | item 8 | A18 | server | One user's attachment is never listed, downloaded or read by another. | `test_rc1.py::test_a_chat_attachment_is_never_seen_by_another_user` |
| RC1-05 | item 5 | A18 | server | An injected tool result is withheld from the model and recorded. | `test_rc1.py::test_an_untrusted_tool_result_is_withheld_from_the_model` |
| RC1-09 | item 9 | A18 | server | A knowledge-graph build is queued on web and run by the worker. | `test_rc1.py::test_a_knowledge_graph_build_is_run_by_the_worker` |
| RC1-07 | item 7 | A18 | server | The standby worker takes the lock when the active one stops. | `test_rc1.py::test_the_standby_worker_takes_over_when_the_active_one_stops` |
| RC1-10 | item 10 | A18 | server | A connection key saved on web is used by web2 without a restart. | `test_rc1.py::test_a_connection_key_saved_on_one_replica_is_used_by_the_other` |
| RC1-04 | item 4 | A18 | server | A personal export imports for one user only; a second import is refused. | `test_rc1.py::test_a_personal_bundle_is_exported_and_imported_for_a_user` |
| RC1-03 | item 3 | A18, Q35 | server | A role allowance stops the plain send route without a model call. | `test_rc1.py::test_a_role_allowance_also_stops_the_plain_send_route` |

## conduit

| Case | Scope | Rulings | Mode | Proves | Test |
|---|---|---|---|---|---|
| CON-2a | scope 2 | D101, D102 | server | Connections of two services discover, take an overlay and refuse by residency. | `test_conduit.py::test_connections_of_two_services_discover_declare_and_refuse` |
| CON-2b | scope 2 | Q54 | server | A model declared on a running connection is usable at once. | `test_conduit.py::test_a_model_declared_on_a_running_connection_can_be_used_at_once` |
| CON-3 | scope 3 | D102 | server | Assignment hides a connection and resolves each allowance. | `test_conduit.py::test_assignment_hides_a_connection_and_decides_each_allowance` |
| CON-4 | scope 4 | D102 | server | Credits warn at 80%, stop at 100% and let the turn in flight finish. | `test_conduit.py::test_credits_warn_at_80_stop_at_100_and_the_turn_in_flight_finishes` |
| CON-5a | scope 5 | A12 | server | An MCP tool over streamable HTTP is called through conduit and audited. | `test_conduit.py::test_an_mcp_tool_over_http_through_conduit_is_called_and_audited` |
| CON-5b | scope 5 | D102 | server | A skill an administrator imports is loaded with use_skill. | `test_conduit.py::test_a_skill_loaded_with_use_skill_reaches_the_model` |
| CON-5c | scope 5 | D102 | server | A forced compaction keeps the conversation usable. | `test_conduit.py::test_a_forced_compaction_keeps_the_conversation_usable` |
| CON-5d | scope 5 | Q55 | server | The stream reports a compaction that ends a turn. | `test_conduit.py::test_the_stream_reports_a_compaction_after_the_reply` |
| CON-6 | scope 6 | D102 | personal | A 0.2 config.yaml becomes two connections once, the key in the keychain. | `test_conduit.py::test_a_0_2_config_with_two_providers_becomes_two_connections_once` |

## live-provider

| Case | Scope | Rulings | Mode | Proves | Test |
|---|---|---|---|---|---|
| LIVE-06 | item 6 | A18 | server | One live chat through the connection named for the run (skipped without one). | `test_live_provider.py::test_one_live_chat_through_the_named_connection` |

## projects-charts

| Case | Scope | Rulings | Mode | Proves | Test |
|---|---|---|---|---|---|
| PCV-1.01 | scope 1 | PC7, PC8 | server | Three levels of instructions reach the prompt top first, nearest wins, the conversation last. | `test_projects_live.py::test_every_level_reaches_the_prompt_top_first_and_the_conversation_last` |
| PCV-1.02 | scope 1 | PC10, PC11 | server | A Contributor's resource is charged to the owner, listed in the prompt and read on demand. | `test_projects_live.py::test_a_contributors_resource_is_charged_to_the_owner_and_read_on_demand` |
| PCV-1.03 | scope 1 | PC9, PC10 | server | Owner, Contributor, Read-only and a non-member each see what their role and scope allow. | `test_projects_live.py::test_each_sharing_scope_decides_what_each_person_sees` |
| PCV-1.04 | scope 1 | PC9 | server | Narrowing a scope hides the project's conversations but keeps an individual share. | `test_projects_live.py::test_a_scope_change_hides_the_projects_conversations_but_not_an_individual_share` |
| PCV-1.05 | scope 1 | PC9, PC16 | server | A project conversation is shared only with the project's people (the picker limit). | `test_projects_live.py::test_a_conversation_in_a_project_is_shared_only_with_its_people` |
| PCV-1.06 | scope 1 | PC12 | server | Moves warn before widening or narrowing, go through no project, and only for the owner. | `test_projects_live.py::test_moves_warn_before_widening_or_narrowing_and_follow_the_rules` |
| PCV-1.07 | scope 1 | PC13 | server | Deleting keeps or deletes conversations, with counts, notices and mail. | `test_projects_live.py::test_deleting_keeps_or_deletes_conversations_with_counts_and_notices` |
| PCV-1.08 | scope 1 | PC14 | server | A transfer moves the tree, its files and their usage; the old owner stays a Contributor. | `test_projects_live.py::test_transfer_moves_the_project_and_its_files_to_the_new_owner` |
| PCV-1.09 | scope 1 | PC11, PC14, Q93 | server | A transfer over the new owner's quota is refused (strict xfail until Q93 is fixed). | `test_projects_live.py::test_transfer_is_refused_when_the_new_owner_lacks_the_quota` |
| PCV-1.10 | scope 1 | PC2 to PC5 | server | The panel lists its three groups and renames by role, audited. | `test_projects_live.py::test_the_panel_lists_three_groups_and_renames_by_role` |
| PCV-1.11 | scope 1 | PC17 | server | create_project creates on the user's request and refuses without one. | `test_projects_live.py::test_create_project_on_request_creates_it_and_refuses_without_one` |
| PCV-1.12 | scope 1 | PC17 | server | A suggestion is a card; after Dismiss no second suggestion is shown. | `test_projects_live.py::test_a_suggestion_is_a_card_dismissed_once_and_never_shown_again` |
| PCV-1.13 | scope 1 | PC18 | server | An update changes nothing until its before-and-after card is accepted, once. | `test_projects_live.py::test_an_update_waits_for_the_owner_to_confirm_its_card` |
| PCV-1.14 | scope 1 | PC15 | both | A personal bundle version 2 imports its projects into a server account, unshared. | `test_personal_live.py::test_a_personal_bundle_v2_carries_its_projects_into_a_server_account` |
| PCV-2.01 | scope 2 | PC5, PC19 | server | Every chart type is made from inline data and built for both themes. | `test_charts_live.py::test_every_chart_type_is_drawn_from_inline_data_in_both_themes` |
| PCV-2.02 | scope 2 | PC19 | server | A chart reads its data from a CSV file and from an Excel sheet. | `test_charts_live.py::test_a_chart_reads_its_data_from_a_csv_and_from_an_excel_file` |
| PCV-2.03 | scope 2 | PC20 | server | A Mermaid diagram is stored with its title and served as its source. | `test_charts_live.py::test_a_diagram_is_stored_with_its_title_and_served_as_its_source` |
| PCV-2.04 | scope 2 | PC20, CS28 | server | A member of the shared conversation opens the charts and data; a stranger cannot. | `test_charts_live.py::test_a_member_of_the_shared_conversation_opens_a_chart_and_a_stranger_cannot` |
| PCV-2.05 | scope 2 | PC19 | server | A refused definition is explained to the model and nothing is stored. | `test_charts_live.py::test_a_definition_the_validator_refuses_is_explained_to_the_model` |
| PCV-2.06 | scope 2 | PC20 | server | In Chromium, cards draw in the message and pane in both themes; PNG, SVG and CSV save as files. | `test_charts_live.py::test_charts_draw_in_the_message_and_the_pane_and_save_as_files` |
| PCV-3.01 | scope 3 | PC15 | personal | Personal projects have levels, resources and moves, and no sharing routes. | `test_personal_live.py::test_personal_projects_have_levels_resources_and_moves_without_sharing` |
| PCV-3.02 | scope 3 | PC2 to PC5, PC19 | personal | Personal charts are listed in the panel with attachments and resources. | `test_personal_live.py::test_personal_charts_and_the_panel` |
| PCV-3.03 | scope 3 | PC21 | personal | Personal mode offers the three built-in skills and loads one. | `test_personal_live.py::test_personal_mode_offers_the_built_in_skills` |
| PCV-4.01 | scope 4 | PC21 | server | The three built-in skills are offered to every user and load. | `test_skills_live.py::test_the_built_in_skills_are_offered_and_load` |
| PCV-4.02 | scope 4 | PC21 | server | An administrator switches a built-in skill off for everyone and on again. | `test_skills_live.py::test_an_administrator_switches_a_built_in_skill_off_and_on_for_everyone` |
| PCV-5.00 | scope 5 | PC16, PC20, Q94 | server | The served chat page starts without a script error (strict xfail until Q94 is fixed). | `test_screens_live.py::test_the_served_chat_page_starts_without_a_script_error` |
| PCV-5.01 | scope 5 | PC4, PC16 | server | The projects, chat and skills screens draw in both themes at desktop and phone width. | `test_screens_live.py::test_the_screens_in_both_themes_at_desktop_and_phone_width` |
