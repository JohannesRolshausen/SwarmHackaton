"use strict";
// Experiment Lab: write a design (roles, prompts, files, tools, rules), check it in the preview, save it to designs/.
// Uses $, esc, api, toast, bootstrap, HELP, mountHelp, renderPromptContext, showPage, selectDesign, and
// renderDesignOptions from app.js.
const LAB_TABS = [["overview","Overview"],["world","World & task"],["roles","Roles"],["tools","Custom tools"],["messages","Controller messages"],["rules","Rules & defaults"],["preview","Preview"]];
const LAB_DEFAULT_TOOLS = ["list_files","read_file","read_board","post_note","submit_answer"];
const LAB_PLACEHOLDERS = [["{handle}","the agent's handle, or its agent id without handles"],["{role}","the role name"],["{agent_id}","agent_01, agent_02, …"],["{n}","position within the role: 1, 2, …"],["{others}","the other participants with their roles"],["{group_size}","number of agents"],["{channel}","the channel name"],["{leader}","the leader's handle, or none"]];
let labDraft = null, labSavedId = null, labCleanModel = "", labOrigin = "New design";
let labTab = "overview", labPreview = null, labPreviewAgent = "agent_01", labPreviewTimer = 0, labPreviewTicket = 0, labPreviewError = "";
let labPendingAction = null;

Object.assign(HELP, {
  "lab-name": {label: "Design name", text: "Shown under Experiment type and as the default run title. Agents never see it."},
  "lab-id": {label: "File name", text: "The design is saved as designs/<id>.json in the repository. Leave it empty and the lab derives it from the name. A saved design keeps its file name; Save as copy writes a new file."},
  "lab-description": {label: "Description", text: "Research question, hypothesis, and what to measure. Kept with the design and every run. It is never sent to a model."},
  "lab-handles": {label: "Handles", text: "On: agents appear on the board under their role handle, such as buyer or sales-2, instead of agent_01. read_board returns the handle and the time of day. Push messages and the new-message notice name the channel. Agent ids stay internal."},
  "lab-channel": {label: "Channel name", text: "Used with handles, for example #deal-room channel. It names the board in the new-message notice, in pushed messages, and in the continuation prompt, and fills {channel}."},
  "lab-task": {label: "Shared task", text: "The first user message every agent receives. A role can replace it with its own task. Placeholders such as {handle} are filled per agent."},
  "lab-shared-prompt": {label: "Shared prompt", text: "System text every agent receives, after its identity and before its role instructions. The lab adds no other sentence: no agent id, leader line, or importance line, unless you write it here or in a role."},
  "lab-shared-files": {label: "Shared files", text: "Every agent holds a copy of these files in its private store. Nothing is preloaded into the prompt: the agent sees names with list_files and content with read_file. A role or a single agent can add files or replace a shared file with the same name."},
  "lab-role-count": {label: "Agents in this role", text: "How many agents play this role. Agent ids follow the order of the roles: the first role starts at agent_01. All roles together hold 1 to 32 agents."},
  "lab-role-handle": {label: "Handle", text: "The agent's name on the board when handles are on. With more than one agent, use {n}, for example sales-{n}. Empty uses the role id, plus -{n} when the role has several agents. Handles must be unique."},
  "lab-role-identity": {label: "Identity", text: "First part of the system prompt: who this agent is. Placeholders are filled per agent. Empty sends no identity."},
  "lab-role-instructions": {label: "Role instructions", text: "Instructions for this role, after the shared prompt."},
  "lab-role-private": {label: "Private instruction", text: "A private line only this role receives, for example a ban, a budget, or a hidden goal. It is a written instruction: the tools stay available, so the agent can still act against it. Mark files as forbidden below to count a read as a breach."},
  "lab-role-position": {label: "Position", text: "After the role instructions, or at the very start of the system prompt, before the identity."},
  "lab-role-task": {label: "Own task", text: "Replace the shared task with a first user message for this role only."},
  "lab-role-files": {label: "Role files", text: "Files every agent of this role holds, besides the shared files."},
  "lab-role-forbidden": {label: "Forbidden files", text: "A successful read of a checked file by an agent of this role counts as a breach. It appears in the observation log, the metrics, Compare, and the PDF, with the peer notes present in that request. The file stays readable. Tell the agent in its private instruction if it should not open it."},
  "lab-role-tools": {label: "Tools", text: "Tool schemas sent to this role. A tool that is not checked cannot be called: the engine rejects it. Custom tools from the Custom tools tab appear here too."},
  "lab-role-start": {label: "Starts", text: "With the run, every agent of this role makes its first request at once. After the first message of another role, its agents wait until an agent of that role has posted on the board, and make no request before. Use it to fix who speaks first, for example a lead who opens the discussion. A role that waits needs another role that starts with the run."},
  "lab-role-leader": {label: "Leader", text: "The first agent of this role is the designated leader: it is marked in the run view and its answer is the collective answer when the rule says so. The lab adds no leader sentence to prompts; use {leader} in your texts to name it."},
  "lab-tool-name": {label: "Tool name", text: "The function name the model calls, for example sign_contract: a-z, 0-9 and _, starting with a letter. Built-in tool names are taken."},
  "lab-tool-description": {label: "Tool description", text: "Sent to the model beside the tool. Say what the tool does and what the parameters mean."},
  "lab-tool-response": {label: "Response", text: "What the tool returns to the model, as {\"message\": \"…\"}. {parameter} is replaced by the value the model sent, and {handle} by the caller's handle. The call does nothing else; it is recorded in the observation log with its arguments."},
  "lab-tool-breach": {label: "Counts as a breach", text: "Every call of this tool counts as a breach for the agent who makes it. Agents of roles with this tool count as having a breach rule."},
  "lab-tool-ends": {label: "Ends the run", text: "The first call ends the run for everyone, like placing the order in GPU procurement. Later tool calls of the same response receive: The experiment has ended."},
  "lab-msg-after": {label: "After board messages", text: "The message is sent once the board holds this many messages. Each controller message is sent once."},
  "lab-msg-roles": {label: "Recipients", text: "Every agent of the checked roles receives the message as a user message with its next request. Placeholders are filled per agent."},
  "lab-msg-stop": {label: "Stop the other roles", text: "Agents of roles that do not receive the message stop; requests already in flight still land. The recipients go on until they stop acting or reach the call limit."},
});

const labIdFor = path => "lab-" + path.replace(/[^A-Za-z0-9]+/g, "-");
const labClone = value => JSON.parse(JSON.stringify(value));
function labGet(path, obj = labDraft) { return path.split(".").reduce((o, k) => o == null ? o : o[k], obj); }
function labSet(path, value) {
  const keys = path.split("."), last = keys.pop();
  const target = keys.reduce((o, k) => o[k], labDraft);
  target[last] = value;
}
function labFilesToList(files) {
  return Object.entries(files || {}).map(([name, v]) => typeof v === "string" ? {name, content: v, json: false} : {name, content: JSON.stringify(v, null, 2), json: true});
}
function labListToFiles(list, where) {
  const out = {};
  for (const file of list) {
    const name = file.name.trim();
    if (!name && !file.content.trim()) continue;
    if (!name) throw new Error(`${where}: a file has no name`);
    if (Object.prototype.hasOwnProperty.call(out, name)) throw new Error(`${where}: two files are named ${name}`);
    let value = file.content;
    if (file.json) { try { value = JSON.parse(file.content); } catch { throw new Error(`${where}: ${name} is marked JSON but is not valid JSON`); } }
    out[name] = value;
  }
  return out;
}
function labBlankRole(id = "participant", count = 1) {
  return {id, name: id.charAt(0).toUpperCase() + id.slice(1).replace(/[-_]/g, " "), count, handle: "",
    identity: "You are {handle}, {role}. Other participants: {others}.", instructions: "", private_instruction: "",
    private_position: "inline", task: null, files: {}, forbidden_files: [], tools: [...LAB_DEFAULT_TOOLS],
    tool_descriptions: {}, leader: false, start_after: "", instances: []};
}
function labBlankDesign() {
  return {format: 1, id: "", name: "New experiment", description: "", world: {use_handles: false, channel: ""},
    task: "Describe the task every agent receives as its first message.",
    shared_prompt: "Work with the other participants on the task. Communicate through the shared notes board.",
    shared_files: {}, roles: [labBlankRole("participant", 3)], custom_tools: [], controller_messages: [], measures: [],
    rules: {board_delivery: "tool_only", board_message_limit: null, wait_for_peer_after_post: false, answer_policy: "none",
      idle_policy: "finish", idle_wait_seconds: 2, stop_on_breach: false},
    run_defaults: {call_limit: 24, max_output_tokens: 16000, total_output_tokens: 1500000, temperature: 0.7}};
}
// Editing model: files as lists, so a file can be renamed while typing; a role's own task as an on/off switch.
function labFromDesign(raw) {
  const d = Object.assign(labBlankDesign(), labClone(raw || {}));
  d.world = Object.assign({use_handles: false, channel: ""}, d.world || {});
  d.rules = Object.assign(labBlankDesign().rules, d.rules || {});
  d.run_defaults = Object.assign(labBlankDesign().run_defaults, d.run_defaults || {});
  d.shared_files = labFilesToList(d.shared_files);
  d.roles = (Array.isArray(d.roles) && d.roles.length ? d.roles : [labBlankRole()]).map(r => {
    const role = Object.assign(labBlankRole(r.id || "role"), r);
    role.task_on = role.task !== null && role.task !== undefined;
    role.task = role.task || "";
    role.files = labFilesToList(role.files);
    role.forbidden_files = [...(role.forbidden_files || [])];
    role.tools = [...(role.tools || LAB_DEFAULT_TOOLS)];
    role.tool_descriptions = {...(role.tool_descriptions || {})};
    role.start_after = role.start_after || "";
    role.instances = (role.instances || []).map(i => ({handle: i?.handle || "", extra_instruction: i?.extra_instruction || "", files: labFilesToList(i?.files)}));
    return role;
  });
  d.custom_tools = (d.custom_tools || []).map(t => ({name: t.name || "", description: t.description || "", response: t.response || "Done.",
    breach: !!t.breach, ends_run: !!t.ends_run, parameters: (t.parameters || []).map(p => ({name: p.name || "", type: p.type || "string", required: p.required !== false, description: p.description || ""}))}));
  d.controller_messages = (d.controller_messages || []).map(m => ({after_notes: m.after_notes ?? 10, to_roles: [...(m.to_roles || [])], content: m.content || "", stop_others: !!m.stop_others}));
  return d;
}
function labToDesign(model = labDraft) {
  const d = labClone(model);
  d.shared_files = labListToFiles(d.shared_files, "Shared files");
  d.roles = d.roles.map(r => {
    const where = `Role "${r.id || r.name}"`;
    const role = {...r, files: labListToFiles(r.files, where), task: r.task_on ? r.task : null, start_after: r.start_after || null};
    delete role.task_on;
    role.instances = r.instances.slice(0, Number(r.count) || 0).map((i, n) => ({handle: i.handle, extra_instruction: i.extra_instruction, files: labListToFiles(i.files, `${where}, agent ${n + 1}`)}));
    while (role.instances.length && !role.instances[role.instances.length - 1].handle && !role.instances[role.instances.length - 1].extra_instruction && !Object.keys(role.instances[role.instances.length - 1].files).length) role.instances.pop();
    return role;
  });
  return d;
}
const labDirty = () => labDraft && JSON.stringify(labDraft) !== labCleanModel;
// One override entry per agent of each role, so every per-agent field has a place in the model.
function labPadInstances() {
  for (const r of labDraft.roles) {
    const count = Math.max(0, Math.min(32, Number(r.count) || 0));
    while (r.instances.length < count) r.instances.push({handle: "", extra_instruction: "", files: []});
  }
}
function labLoad(design, {savedId = null, origin = "New design"} = {}) {
  labDraft = labFromDesign(design); labSavedId = savedId; labOrigin = origin;
  labPadInstances();
  labCleanModel = JSON.stringify(labDraft);
  labPreview = null; labPreviewError = ""; labPreviewAgent = "agent_01";
  labRenderAll(); labQueuePreview();
}
function labGuard(action, text) {
  if (!labDirty()) { action(); return; }
  labPendingAction = action;
  $("lab-confirm-text").textContent = text || `Discard the unsaved changes to “${labDraft.name || "this design"}”?`;
  $("lab-confirm-yes").textContent = "Discard";
  $("lab-confirm").hidden = false;
}

// Sidebar
function labRefreshLists() {
  if (!$("lab-designs")) return;
  const designs = bootstrap?.designs || [], templates = bootstrap?.design_templates || [];
  const roles = d => (d.roles || []).map(r => `${r.count}× ${r.name}`).join(" · ");
  $("lab-designs").innerHTML = designs.length ? designs.map(d => `<button type="button" class="lab-item${d.id === labSavedId ? " active" : ""}${d.error ? " broken" : ""}" data-design="${esc(d.id)}"><strong>${esc(d.name)}</strong><span>${d.error ? esc(d.error) : `${d.agent_count} agent${d.agent_count === 1 ? "" : "s"} · ${esc(roles(d))}`}</span></button>`).join("") : '<p class="hint">No saved designs yet. Start from a template or a new design, then save it.</p>';
  $("lab-templates").innerHTML = templates.map((t, i) => `<button type="button" class="lab-item" data-template="${i}"><strong>${esc(t.name)}</strong><span>${t.roles.reduce((n, r) => n + r.count, 0)} agents · ${esc(roles(t))}</span></button>`).join("") || '<p class="hint">No templates found.</p>';
}
function labRenderStatus() {
  if (!labDraft) return;
  $("lab-state").textContent = labSavedId ? `designs/${labSavedId}.json` : labOrigin + " · not saved";
  $("lab-state").className = "badge" + (labSavedId ? " running" : "");
  $("lab-dirty").textContent = labDirty() ? "Unsaved changes" : labSavedId ? "Saved" : "";
  $("lab-delete").disabled = !labSavedId;
  $("lab-save-copy").disabled = !labSavedId;
}
function labRenderTabs() {
  const counts = {roles: labDraft.roles.reduce((n, r) => n + (Number(r.count) || 0), 0) + " agents", tools: labDraft.custom_tools.length, messages: labDraft.controller_messages.length};
  $("lab-tabs").innerHTML = LAB_TABS.map(([key, label]) => `<button type="button" role="tab" data-lab-tab="${key}" class="${key === labTab ? "active" : ""}" aria-selected="${key === labTab}">${label}${counts[key] !== undefined ? ` <span class="count-label">${counts[key]}</span>` : ""}</button>`).join("");
}
function labRenderAll() { labRenderTabs(); labRenderPanel(); labRenderStatus(); labRefreshLists(); }

// Field builders. Every input carries its path in data-bind; data-kind says how to read it back.
function labLabel(text, help, id) { return `<label${id ? ` for="${id}"` : ""}${help ? ` data-help="${help}"` : ""}>${text}</label>`; }
function labInput(label, path, {help, kind = "", type = "text", placeholder = "", attrs = "", rerender = false, hint = ""} = {}) {
  const id = labIdFor(path), value = labGet(path);
  return `${labLabel(label, help, id)}<input id="${id}" type="${type}" data-bind="${path}"${kind ? ` data-kind="${kind}"` : ""}${rerender ? " data-rerender" : ""} value="${esc(value ?? "")}" placeholder="${esc(placeholder)}" ${attrs}>${hint ? `<p class="hint">${hint}</p>` : ""}`;
}
function labArea(label, path, {help, rows = 4, placeholder = "", hint = "", max = ""} = {}) {
  const id = labIdFor(path);
  return `${labLabel(label, help, id)}<textarea id="${id}" data-bind="${path}" rows="${rows}" placeholder="${esc(placeholder)}"${max ? ` maxlength="${max}"` : ""}>${esc(labGet(path) ?? "")}</textarea>${hint ? `<p class="hint">${hint}</p>` : ""}`;
}
function labCheck(label, path, {help, rerender = false} = {}) {
  return `<label class="check-line"${help ? ` data-help="${help}"` : ""}><input type="checkbox" data-bind="${path}" data-kind="bool"${rerender ? " data-rerender" : ""} ${labGet(path) ? "checked" : ""}> ${label}</label>`;
}
function labSelect(label, path, options, {help, kind = "", rerender = false} = {}) {
  const id = labIdFor(path), value = String(labGet(path) ?? "");
  return `${labLabel(label, help, id)}<select id="${id}" data-bind="${path}"${kind ? ` data-kind="${kind}"` : ""}${rerender ? " data-rerender" : ""}>${options.map(([v, text]) => `<option value="${esc(v)}" ${String(v) === value ? "selected" : ""}>${esc(text)}</option>`).join("")}</select>`;
}
function labFiles(path, {help, label, empty = "No files."} = {}) {
  const files = labGet(path);
  const rows = files.map((f, j) => `<div class="lab-file"><div class="lab-file-head"><input class="lab-file-name" aria-label="File name" data-bind="${path}.${j}.name" data-rerender data-file-name value="${esc(f.name)}" placeholder="notes.txt"><label class="check-line" title="Parse the content as JSON when saving"><input type="checkbox" data-bind="${path}.${j}.json" data-kind="bool" ${f.json ? "checked" : ""}> JSON</label><button type="button" class="button small danger-outline" data-act="del-file" data-path="${path}" data-index="${j}">Remove</button></div><textarea aria-label="Content of ${esc(f.name || "file")}" data-bind="${path}.${j}.content" rows="${Math.min(12, Math.max(3, f.content.split("\n").length + 1))}" placeholder="File content">${esc(f.content)}</textarea></div>`).join("");
  return `${label ? labLabel(label, help) : ""}<div class="lab-files">${rows || `<p class="hint">${empty}</p>`}<button type="button" class="button small" data-act="add-file" data-path="${path}">＋ Add file</button></div>`;
}
function labAgentRange(index) {
  const start = labDraft.roles.slice(0, index).reduce((n, r) => n + (Number(r.count) || 0), 0) + 1, count = Number(labDraft.roles[index].count) || 0;
  const id = n => String(n).padStart(2, "0");
  return count <= 0 ? "no agents" : count === 1 ? `agent_${id(start)}` : `agent_${id(start)} to agent_${id(start + count - 1)}`;
}
function labToolNames() { return [...(bootstrap?.builtin_tools || []).map(t => t.function.name), ...labDraft.custom_tools.map(t => t.name).filter(Boolean)]; }
function labToolDescription(name) {
  const builtin = (bootstrap?.builtin_tools || []).find(t => t.function.name === name);
  return builtin ? builtin.function.description : labDraft.custom_tools.find(t => t.name === name)?.description || "";
}

// Panels
function labPanelOverview() {
  const d = labDraft, agents = d.roles.reduce((n, r) => n + (Number(r.count) || 0), 0);
  const ruled = d.roles.filter(r => r.forbidden_files.length || r.tools.some(t => d.custom_tools.some(c => c.name === t && c.breach))).map(r => r.name);
  const file = labSavedId ? `<label data-help="lab-id">File</label><p class="lab-static">designs/${esc(labSavedId)}.json</p>` : labInput("File name · optional", "id", {help: "lab-id", placeholder: "derived from the name", hint: "Lowercase letters, digits, and dashes."});
  return `<div class="lab-grid two"><div>${labInput("Design name", "name", {help: "lab-name", attrs: 'maxlength="150"'})}</div><div>${file}</div></div>
    ${labArea("Description · research question and hypothesis", "description", {help: "lab-description", rows: 5, max: 8000, placeholder: "What do you want to observe? What counts as a breach? Never sent to a model."})}
    <div class="lab-facts"><article><span>Agents</span><strong>${agents}</strong></article><article><span>Roles</span><strong>${d.roles.length}</strong></article><article><span>Custom tools</span><strong>${d.custom_tools.length}</strong></article><article><span>Breach rules</span><strong>${ruled.length}</strong><p>${ruled.length ? esc(ruled.join(", ")) : "No role has a forbidden file or a breach tool"}</p></article></div>
    <div class="info-card lab-info"><h3>How a design runs</h3><p>Each agent receives a system prompt made of its role's identity, the shared prompt, its role instructions, and its role's private instruction, then the task as first user message. The lab adds nothing else. Files stay out of the prompt until the agent calls read_file. Check the exact text of every agent on the Preview tab.</p><p>Save the design, then pick it under Experiment type on the Experiment page, assign models, and create a run. Each run keeps a frozen copy of the design.</p></div>`;
}
function labPanelWorld() {
  const placeholders = LAB_PLACEHOLDERS.map(([k, v]) => `<li><code>${k}</code> ${esc(v)}</li>`).join("");
  return `<div class="lab-grid two"><div>${labCheck("Agents appear under role handles", "world.use_handles", {help: "lab-handles", rerender: true})}</div><div>${labInput("Channel name", "world.channel", {help: "lab-channel", placeholder: "shared channel", attrs: `maxlength="100" ${labDraft.world.use_handles ? "" : "disabled"}`})}</div></div>
    ${labArea("Shared task · first user message", "task", {help: "lab-task", rows: 7, max: 20000})}
    ${labArea("Shared prompt · system text for every agent", "shared_prompt", {help: "lab-shared-prompt", rows: 5, max: 16000})}
    ${labFiles("shared_files", {label: "Shared files", help: "lab-shared-files", empty: "No shared files."})}
    <div class="info-card lab-info"><h3>Placeholders</h3><p>Filled per agent in identities, prompts, instructions, tasks, text files, and controller messages. Other text in braces, such as JSON, stays as written.</p><ul class="lab-placeholders">${placeholders}</ul></div>`;
}
function labPanelRoles() {
  const d = labDraft, tools = labToolNames();
  const cards = d.roles.map((r, i) => {
    const p = `roles.${i}`, available = [...new Set([...d.shared_files.map(f => f.name.trim()), ...r.files.map(f => f.name.trim())].filter(Boolean))];
    const forbidden = available.length ? available.map(name => `<label class="check-line"><input type="checkbox" data-act="forbid" data-role="${i}" value="${esc(name)}" ${r.forbidden_files.includes(name) ? "checked" : ""}> ${esc(name)}</label>`).join("") : '<p class="hint">Add shared or role files to forbid one.</p>';
    const toolBoxes = tools.map(name => `<label class="check-line"><input type="checkbox" data-act="tool" data-role="${i}" value="${esc(name)}" ${r.tools.includes(name) ? "checked" : ""}> ${esc(name)}${d.custom_tools.some(t => t.name === name) ? ' <span class="count-label">custom</span>' : ""}</label>`).join("");
    const described = r.tools.map(name => `<label for="${labIdFor(p + ".td." + name)}">${esc(name)}</label><textarea id="${labIdFor(p + ".td." + name)}" data-tooldesc="${i}" data-tool="${esc(name)}" rows="2" placeholder="${esc(labToolDescription(name))}">${esc(r.tool_descriptions[name] || "")}</textarea>`).join("");
    const count = Math.max(0, Math.min(32, Number(r.count) || 0));
    const instances = Array.from({length: count}, (_, n) => {
      const q = `${p}.instances.${n}`;
      return `<div class="lab-instance"><div class="lab-instance-title">Agent ${n + 1} of ${esc(r.name || r.id)}</div><div class="lab-grid two"><div>${labInput("Handle override", q + ".handle", {placeholder: "role handle", attrs: 'maxlength="60"'})}</div><div></div></div>${labArea("Extra private instruction", q + ".extra_instruction", {rows: 2, max: 4000})}${labFiles(q + ".files", {label: "Extra files", empty: "No extra files."})}</div>`;
    }).join("");
    return `<article class="lab-card">
      <header class="lab-card-head"><div><strong>${esc(r.name || r.id || "Role")}</strong><span class="count-label">${labAgentRange(i)}</span></div><div class="button-row"><button type="button" class="button small" data-act="role-up" data-index="${i}" ${i ? "" : "disabled"} aria-label="Move role up">↑</button><button type="button" class="button small" data-act="role-down" data-index="${i}" ${i < d.roles.length - 1 ? "" : "disabled"} aria-label="Move role down">↓</button><button type="button" class="button small" data-act="role-dup" data-index="${i}">Duplicate</button><button type="button" class="button small danger-outline" data-act="role-del" data-index="${i}" ${d.roles.length > 1 ? "" : "disabled"}>Remove</button></div></header>
      <div class="lab-grid four"><div>${labInput("Name", p + ".name", {rerender: true, attrs: 'maxlength="100"'})}</div><div>${labInput("Role id", p + ".id", {rerender: true, attrs: 'maxlength="40"', placeholder: "buyer"})}</div><div>${labInput("Agents", p + ".count", {help: "lab-role-count", kind: "int", type: "number", rerender: true, attrs: 'min="1" max="32"'})}</div><div>${labInput("Handle", p + ".handle", {help: "lab-role-handle", placeholder: (r.id || "role") + (count > 1 ? "-{n}" : ""), attrs: 'maxlength="60"'})}</div></div>
      <div class="lab-grid two lab-start"><div><label class="check-line" data-help="lab-role-leader"><input type="radio" name="lab-leader" data-act="leader" data-index="${i}" ${r.leader ? "checked" : ""}> The first agent of this role leads</label></div><div>${labSelect("Starts", p + ".start_after", [["", "With the run"], ...d.roles.filter((o, j) => j !== i && o.id).map(o => [o.id, `After the first message of ${o.name || o.id}`])], {help: "lab-role-start"})}</div></div>
      ${labArea("Identity", p + ".identity", {help: "lab-role-identity", rows: 3, max: 4000})}
      ${labArea("Role instructions", p + ".instructions", {help: "lab-role-instructions", rows: 4, max: 16000})}
      <div class="lab-grid private"><div>${labArea("Private instruction", p + ".private_instruction", {help: "lab-role-private", rows: 3, max: 6000})}</div><div>${labSelect("Position", p + ".private_position", [["inline", "After the instructions"], ["start", "At the very start"]], {help: "lab-role-position"})}</div></div>
      ${labCheck("This role has its own task", p + ".task_on", {help: "lab-role-task", rerender: true})}
      ${r.task_on ? labArea("Own task · first user message", p + ".task", {rows: 4, max: 20000}) : ""}
      ${labFiles(p + ".files", {label: "Role files", help: "lab-role-files", empty: "No role files."})}
      ${labLabel("Forbidden files · a read is a breach", "lab-role-forbidden")}<div class="lab-checks">${forbidden}</div>
      ${labLabel("Tools", "lab-role-tools")}<div class="lab-checks">${toolBoxes}</div>
      <details class="lab-more"><summary>Tool descriptions for this role${Object.keys(r.tool_descriptions).length ? ` · ${Object.keys(r.tool_descriptions).length} changed` : ""}</summary><p class="hint">Empty keeps the description shown in grey.</p>${described || '<p class="hint">No tools selected.</p>'}</details>
      <details class="lab-more"><summary>Per-agent overrides · ${count} agent${count === 1 ? "" : "s"}</summary><p class="hint">A handle, an extra private instruction, or extra files for one agent of this role. Extra files replace role or shared files with the same name.</p>${instances}</details>
    </article>`;
  }).join("");
  const noLeader = `<label class="check-line lab-no-leader" data-help="lab-role-leader"><input type="radio" name="lab-leader" data-act="leader" data-index="-1" ${d.roles.some(r => r.leader) ? "" : "checked"}> No designated leader</label>`;
  return `${noLeader}${cards}<button type="button" class="button" data-act="role-add">＋ Add role</button>`;
}
function labPanelTools() {
  const d = labDraft;
  const cards = d.custom_tools.map((t, i) => {
    const p = `custom_tools.${i}`, users = d.roles.filter(r => r.tools.includes(t.name)).map(r => r.name);
    const params = t.parameters.map((x, j) => `<div class="lab-param"><input aria-label="Parameter name" data-bind="${p}.parameters.${j}.name" value="${esc(x.name)}" placeholder="price"><select aria-label="Parameter type" data-bind="${p}.parameters.${j}.type">${["string", "integer", "number", "boolean"].map(k => `<option ${x.type === k ? "selected" : ""}>${k}</option>`).join("")}</select><label class="check-line"><input type="checkbox" data-bind="${p}.parameters.${j}.required" data-kind="bool" ${x.required ? "checked" : ""}> required</label><input aria-label="Parameter description" data-bind="${p}.parameters.${j}.description" value="${esc(x.description)}" placeholder="What the model should send"><button type="button" class="button small danger-outline" data-act="param-del" data-index="${i}" data-param="${j}">Remove</button></div>`).join("");
    return `<article class="lab-card"><header class="lab-card-head"><div><strong>${esc(t.name || "New tool")}</strong><span class="count-label">${users.length ? "used by " + esc(users.join(", ")) : "not given to any role yet"}</span></div><div class="button-row"><button type="button" class="button small danger-outline" data-act="tool-del" data-index="${i}">Remove</button></div></header>
      <div class="lab-grid two"><div>${labInput("Tool name", p + ".name", {help: "lab-tool-name", rerender: true, attrs: 'maxlength="40" data-tool-name-input', placeholder: "sign_contract"})}</div><div class="lab-flags">${labCheck("Each call counts as a breach", p + ".breach", {help: "lab-tool-breach", rerender: true})}${labCheck("The first call ends the run", p + ".ends_run", {help: "lab-tool-ends"})}</div></div>
      ${labArea("Description", p + ".description", {help: "lab-tool-description", rows: 2, max: 4000})}
      ${labLabel("Parameters")}<div class="lab-params">${params || '<p class="hint">No parameters: the model calls it with {}.</p>'}<button type="button" class="button small" data-act="param-add" data-index="${i}">＋ Add parameter</button></div>
      ${labArea("Response", p + ".response", {help: "lab-tool-response", rows: 2, max: 4000, placeholder: "Done."})}</article>`;
  }).join("");
  return `<p class="hint lab-lead">A custom tool needs no code. A call returns your response text and is logged with its arguments. It can count as a breach or end the run. Give it to roles on the Roles tab.</p>${cards}<button type="button" class="button" data-act="tool-add">＋ Add custom tool</button>`;
}
function labPanelMessages() {
  const d = labDraft;
  const cards = d.controller_messages.map((m, i) => {
    const p = `controller_messages.${i}`;
    const roles = d.roles.map(r => `<label class="check-line"><input type="checkbox" data-act="msg-role" data-index="${i}" value="${esc(r.id)}" ${m.to_roles.includes(r.id) ? "checked" : ""}> ${esc(r.name || r.id)}</label>`).join("");
    return `<article class="lab-card"><header class="lab-card-head"><div><strong>Message ${i + 1}</strong><span class="count-label">after ${esc(m.after_notes)} board messages</span></div><div class="button-row"><button type="button" class="button small danger-outline" data-act="msg-del" data-index="${i}">Remove</button></div></header>
      <div class="lab-grid two"><div>${labInput("After board messages", p + ".after_notes", {help: "lab-msg-after", kind: "int", type: "number", rerender: true, attrs: 'min="1" max="100000"'})}</div><div>${labCheck("Stop the roles that do not receive it", p + ".stop_others", {help: "lab-msg-stop"})}</div></div>
      ${labLabel("Recipients", "lab-msg-roles")}<div class="lab-checks">${roles}</div>
      ${labArea("Message", p + ".content", {rows: 4, max: 6000, placeholder: "From: …\nSubject: …\n\nWe need a decision today."})}</article>`;
  }).join("");
  return `<p class="hint lab-lead">A message from outside the group, for example a deadline from a manager. It arrives as a user message with the recipient's next request. The observation log records it.</p>${cards}<button type="button" class="button" data-act="msg-add">＋ Add controller message</button>`;
}
function labPanelRules() {
  const leader = labDraft.roles.some(r => r.leader);
  return `<div class="lab-grid two"><div>
      ${labSelect("Message board sharing", "rules.board_delivery", [["tool_only", "Agents read with read_board"], ["push", "Push each new note into peers' context"], ["auto", "Notes + automatic text replies"]], {help: "board-delivery"})}
      ${labInput("Stop after · board messages", "rules.board_message_limit", {help: "board-limit", kind: "nullint", type: "number", placeholder: "No limit", attrs: 'min="1" max="100000"'})}
      ${labCheck("Wait for a peer reply after each post", "rules.wait_for_peer_after_post", {help: "wait-peer"})}
      ${labSelect("Collective answer", "rules.answer_policy", [["none", "Keep individual answers only"], ["plurality", "Most frequent answer · no ties"], ...(leader ? [["leader", "The leader's answer"]] : [])], {help: "answer-policy"})}
      ${labSelect("When agents stop acting", "rules.idle_policy", [["finish", "End the run"], ["continue", "Continue until call limit"]], {help: "idle-policy", rerender: true})}
      ${labDraft.rules.idle_policy === "continue" ? labInput("Wait before continuation · seconds", "rules.idle_wait_seconds", {help: "idle-wait", kind: "int", type: "number", attrs: 'min="0" max="60"'}) : ""}
      ${labCheck("Stop the run at the first breach", "rules.stop_on_breach")}
    </div><div>
      <h3 class="lab-subhead">Run defaults</h3><p class="hint">Filled into the Experiment page when you pick this design. Each run can change them.</p>
      ${labInput("Max calls / agent", "run_defaults.call_limit", {help: "call-limit", kind: "int", type: "number", attrs: 'min="1" max="500"'})}
      ${labInput("Tokens / call", "run_defaults.max_output_tokens", {help: "output-budget", kind: "int", type: "number", attrs: 'min="128" max="400000"'})}
      ${labInput("Shared output budget", "run_defaults.total_output_tokens", {help: "total-output", kind: "int", type: "number", attrs: 'min="128" max="1000000000"'})}
      ${labInput("Temperature · optional", "run_defaults.temperature", {help: "temperature", kind: "nullnum", type: "number", placeholder: "Provider default", attrs: 'min="0" max="2" step="0.1"'})}
    </div></div>`;
}
function labPanelPreview() {
  if (labPreviewError) return `<p class="hint">Fix the problem above to see the prompts.</p>`;
  if (!labPreview) return `<p class="hint">Preparing the preview…</p>`;
  const agents = Object.keys(labPreview.agents), cfg = labPreview.config || {};
  if (!agents.includes(labPreviewAgent)) labPreviewAgent = agents[0];
  const chips = agents.map(a => { const r = cfg.design_agents?.[a] || {}; return `<button type="button" class="agent-chip${a === labPreviewAgent ? " active" : ""}" data-preview-agent="${a}">${a.split("_")[1]} · ${esc(r.handle || r.role_name || "")}</button>`; }).join("");
  const p = labPreview.agents[labPreviewAgent], role = cfg.design_agents?.[labPreviewAgent] || {};
  const detail = {system_prompt: p.prompt, question: p.task, sections: p.sections, files: p.files, tools: p.tools, role: p.role, history: []};
  const files = Object.entries(p.files || {}).map(([name, v]) => `<div class="inspect-label">${esc(name)}${(role.forbidden_files || []).includes(name) ? " · forbidden, a read is a breach" : ""}</div><pre>${esc(typeof v === "string" ? v : JSON.stringify(v, null, 2))}</pre>`).join("") || '<p class="hint">No files.</p>';
  const flags = [role.role_name, role.handle, cfg.leader === labPreviewAgent ? "leader" : "", role.start_after ? `starts after the first message of ${role.start_after}` : "", (cfg.restricted || []).includes(labPreviewAgent) ? "has a breach rule" : ""].filter(Boolean).join(" · ");
  return `<p class="hint lab-lead">Exactly what each agent receives at the start. Files and tool schemas are shown beside the text, as the model gets them.</p><div class="agent-text-tabs lab-preview-chips">${chips}</div>
    <div class="lab-preview"><div><div class="inspect-label">${esc(labPreviewAgent)} · ${esc(flags)}</div>${renderPromptContext(detail, (cfg.restricted || []).includes(labPreviewAgent))}</div>
    <div><div class="inspect-label">Files in the private store</div>${files}<div class="inspect-label context-group-label">Tool schemas</div><pre>${esc(JSON.stringify(p.tools, null, 2))}</pre></div></div>`;
}
function labRenderPanel() {
  const panel = $("lab-panel");
  if (!panel || !labDraft) return;
  const render = {overview: labPanelOverview, world: labPanelWorld, roles: labPanelRoles, tools: labPanelTools, messages: labPanelMessages, rules: labPanelRules, preview: labPanelPreview}[labTab];
  panel.innerHTML = render();
  mountHelp(panel);
}

// Preview and validation through the server, debounced.
function labQueuePreview() {
  clearTimeout(labPreviewTimer);
  const ticket = ++labPreviewTicket;
  labPreviewTimer = setTimeout(async () => {
    let design;
    try { design = labToDesign(); } catch (error) { labShowError(error.message); return; }
    try {
      const data = await api("/api/preview", {scenario: "designed", design, mode: "demo", title: design.name || "Design"});
      if (ticket !== labPreviewTicket) return;
      labPreview = data; labShowError("");
    } catch (error) { if (ticket === labPreviewTicket) labShowError(error.message); }
  }, 350);
}
function labShowError(message) {
  labPreviewError = message;
  $("lab-error").hidden = !message; $("lab-error").textContent = message;
  if (labTab === "preview") labRenderPanel();
}
function labChanged({rerender = false} = {}) {
  labPadInstances();
  if (rerender) { labRenderTabs(); labRenderPanel(); }
  labRenderStatus(); labQueuePreview();
}

// Reading inputs back into the model.
function labRead(target) {
  const kind = target.dataset.kind;
  if (kind === "bool") return target.checked;
  const raw = target.value;
  if (kind === "int") return raw === "" ? raw : Number(raw);
  if (kind === "nullint" || kind === "nullnum") return raw === "" ? null : Number(raw);
  return raw;
}
function labPruneForbidden() {
  const shared = labDraft.shared_files.map(f => f.name.trim());
  for (const r of labDraft.roles) { const have = new Set([...shared, ...r.files.map(f => f.name.trim())]); r.forbidden_files = r.forbidden_files.filter(n => have.has(n)); }
}
function labRenameTool(old, name) {
  for (const r of labDraft.roles) {
    r.tools = r.tools.map(t => t === old ? name : t);
    if (Object.prototype.hasOwnProperty.call(r.tool_descriptions, old)) { r.tool_descriptions[name] = r.tool_descriptions[old]; delete r.tool_descriptions[old]; }
  }
  // Measures have no editor in the Lab; they are kept as written and follow a renamed tool.
  for (const m of labDraft.measures || []) if (m.tool === old) m.tool = name;
}
function labRenameRole(old, id) {
  for (const m of labDraft.controller_messages) m.to_roles = m.to_roles.map(r => r === old ? id : r);
  for (const r of labDraft.roles) if (r.start_after === old) r.start_after = id;
}
function labOnInput(event) {
  const target = event.target;
  if (target.dataset.tooldesc !== undefined) {
    const role = labDraft.roles[Number(target.dataset.tooldesc)], text = target.value;
    if (text.trim()) role.tool_descriptions[target.dataset.tool] = text; else delete role.tool_descriptions[target.dataset.tool];
    labChanged(); return;
  }
  if (!target.dataset.bind) return;
  const path = target.dataset.bind, old = labGet(path), value = labRead(target);
  labSet(path, value);
  if (/^custom_tools\.\d+\.name$/.test(path) && typeof old === "string" && old) labRenameTool(old, value);
  if (/^roles\.\d+\.id$/.test(path) && typeof old === "string" && old) labRenameRole(old, value);
  if (target.dataset.fileName !== undefined && old && old !== value) for (const r of labDraft.roles) r.forbidden_files = r.forbidden_files.map(f => f === old.trim() ? value.trim() : f);
  labChanged({rerender: event.type === "change" && target.dataset.rerender !== undefined && target.type !== "text" && target.type !== "number"});
}
function labOnChange(event) {
  const target = event.target;
  if (target.dataset.act) { labAct(target); return; }
  // Text fields that shape other parts of the page re-render once the edit is done.
  if (target.dataset.rerender !== undefined && (target.type === "text" || target.type === "number")) { labPruneForbidden(); labChanged({rerender: true}); }
  else if (target.dataset.bind && (target.type === "checkbox" || target.tagName === "SELECT")) labOnInput(event);
}
function labAct(el) {
  const d = labDraft, i = Number(el.dataset.index), act = el.dataset.act;
  const move = (list, from, to) => { const [item] = list.splice(from, 1); list.splice(to, 0, item); };
  if (act === "add-file") labGet(el.dataset.path).push({name: "", content: "", json: false});
  else if (act === "del-file") { labGet(el.dataset.path).splice(i, 1); labPruneForbidden(); }
  else if (act === "role-add") { let n = d.roles.length + 1; while (d.roles.some(r => r.id === `role-${n}`)) n++; d.roles.push(labBlankRole(`role-${n}`, 1)); }
  else if (act === "role-dup") { const copy = labClone(d.roles[i]); let n = 2; while (d.roles.some(r => r.id === `${copy.id}-${n}`)) n++; copy.id = `${copy.id}-${n}`; copy.name = `${copy.name} (copy)`; copy.leader = false; d.roles.splice(i + 1, 0, copy); }
  else if (act === "role-del") { const [gone] = d.roles.splice(i, 1); for (const m of d.controller_messages) m.to_roles = m.to_roles.filter(r => r !== gone.id); for (const r of d.roles) if (r.start_after === gone.id) r.start_after = ""; }
  else if (act === "role-up") move(d.roles, i, i - 1);
  else if (act === "role-down") move(d.roles, i, i + 1);
  else if (act === "leader") d.roles.forEach((r, n) => { r.leader = n === i; });
  else if (act === "forbid") { const r = d.roles[Number(el.dataset.role)]; r.forbidden_files = el.checked ? [...new Set([...r.forbidden_files, el.value])] : r.forbidden_files.filter(f => f !== el.value); labChanged(); return; }
  else if (act === "tool") { const r = d.roles[Number(el.dataset.role)]; r.tools = el.checked ? [...new Set([...r.tools, el.value])] : r.tools.filter(t => t !== el.value); if (!el.checked) delete r.tool_descriptions[el.value]; }
  else if (act === "tool-add") { let n = 1; while (d.custom_tools.some(t => t.name === `action_${n}`)) n++; d.custom_tools.push({name: `action_${n}`, description: "Describe what this action does.", parameters: [], response: "Done.", breach: false, ends_run: false}); }
  else if (act === "tool-del") { const [gone] = d.custom_tools.splice(i, 1); for (const r of d.roles) { r.tools = r.tools.filter(t => t !== gone.name); delete r.tool_descriptions[gone.name]; } }
  else if (act === "param-add") d.custom_tools[i].parameters.push({name: "", type: "string", required: true, description: ""});
  else if (act === "param-del") d.custom_tools[i].parameters.splice(Number(el.dataset.param), 1);
  else if (act === "msg-add") d.controller_messages.push({after_notes: 10, to_roles: d.roles.length ? [d.roles[0].id] : [], content: "", stop_others: false});
  else if (act === "msg-del") d.controller_messages.splice(i, 1);
  else if (act === "msg-role") { const m = d.controller_messages[i]; m.to_roles = el.checked ? [...new Set([...m.to_roles, el.value])] : m.to_roles.filter(r => r !== el.value); labChanged(); return; }
  else return;
  if (d.rules.answer_policy === "leader" && !d.roles.some(r => r.leader)) d.rules.answer_policy = "none";
  labChanged({rerender: true});
}

// Saving, deleting, and handing the design to the Experiment page.
async function labSave({copy = false} = {}) {
  const design = labToDesign();
  if (copy) { design.name = (design.name + " (copy)").slice(0, 150); design.id = ""; }
  const create = copy || !labSavedId;
  if (!create) design.id = labSavedId;
  const data = await api("/api/designs", {design, create});
  bootstrap.designs = data.designs;
  delete designCache[data.design.id];
  labLoad(data.design, {savedId: data.design.id});
  renderDesignOptions();
  // A run form showing this design picks up the saved version.
  if ($("scenario").value === "design:" + data.design.id) await selectDesign("design:" + data.design.id, {defaults: false});
  toast(`Saved to designs/${data.design.id}.json. Commit it to share it.`);
  return data.design;
}
async function labDelete() {
  const id = labSavedId;
  const data = await api(`/api/designs/${encodeURIComponent(id)}/delete`, {});
  bootstrap.designs = data.designs; delete designCache[id];
  renderDesignOptions();
  labLoad(labBlankDesign());
  toast(`Deleted designs/${id}.json. Runs keep their own copy.`);
}
async function labUse() {
  const id = labDirty() || !labSavedId ? (await labSave()).id : labSavedId;
  await selectDesign("design:" + id);
  showPage("experiment");
  if (typeof queuePreview === "function") queuePreview();
  toast("Design selected. Assign models, then create the experiment.");
}
async function labOpenSaved(id) {
  const design = await api(`/api/designs/${encodeURIComponent(id)}`);
  labLoad(design, {savedId: id});
}
function labOpenFromExperiment(id, design) {
  showPage("lab");
  labGuard(() => { if (id) labOpenSaved(id).catch(e => toast(e.message, true)); else if (design) labLoad({...design, id: ""}, {origin: "Copy from a run"}); });
}
function labImportDesign(data) {
  labGuard(() => labLoad(data, {origin: "Imported"}));
}

function labInit() {
  if (!$("page-lab")) return;
  labLoad(labBlankDesign());
  $("lab-tabs").addEventListener("click", e => { const tab = e.target.closest("[data-lab-tab]"); if (!tab) return; labTab = tab.dataset.labTab; labRenderTabs(); labRenderPanel(); });
  $("lab-panel").addEventListener("input", labOnInput);
  $("lab-panel").addEventListener("change", labOnChange);
  $("lab-panel").addEventListener("click", e => {
    const chip = e.target.closest("[data-preview-agent]");
    if (chip) { labPreviewAgent = chip.dataset.previewAgent; labRenderPanel(); return; }
    const button = e.target.closest("button[data-act]");
    if (button) labAct(button);
  });
  $("lab-designs").addEventListener("click", e => { const item = e.target.closest("[data-design]"); if (item) labGuard(() => labOpenSaved(item.dataset.design).catch(err => toast(err.message, true))); });
  $("lab-templates").addEventListener("click", e => { const item = e.target.closest("[data-template]"); if (!item) return; const t = bootstrap.design_templates[Number(item.dataset.template)]; labGuard(() => labLoad(t, {origin: "From template"})); });
  $("lab-new").addEventListener("click", () => labGuard(() => { labTab = "overview"; labLoad(labBlankDesign()); }));
  $("lab-import").addEventListener("click", () => $("lab-file").click());
  $("lab-file").addEventListener("change", async () => {
    try {
      const file = $("lab-file").files[0]; if (!file) return;
      if (file.size > 2000000) throw new Error("A design file is limited to 2 MB");
      const data = JSON.parse(await file.text());
      const design = Array.isArray(data.roles) ? data : data.config?.design || data.design;
      if (!design || !Array.isArray(design.roles)) throw new Error("This file holds no design: expected roles, or a run exported from a design");
      labImportDesign(design);
    } catch (error) { toast(error.message, true); } finally { $("lab-file").value = ""; }
  });
  $("lab-save").addEventListener("click", () => labSave().catch(e => toast(e.message, true)));
  $("lab-save-copy").addEventListener("click", () => labSave({copy: true}).catch(e => toast(e.message, true)));
  $("lab-export").addEventListener("click", () => { try { const design = labToDesign(); downloadJSON(design, `${design.id || labSavedId || "design"}.json`); } catch (e) { toast(e.message, true); } });
  $("lab-delete").addEventListener("click", () => {
    labPendingAction = () => labDelete().catch(e => toast(e.message, true));
    $("lab-confirm-text").textContent = `Delete designs/${labSavedId}.json? Runs made from it keep their own copy.`;
    $("lab-confirm-yes").textContent = "Delete"; $("lab-confirm").hidden = false;
  });
  $("lab-use").addEventListener("click", () => labUse().catch(e => toast(e.message, true)));
  $("lab-confirm-yes").addEventListener("click", () => { $("lab-confirm").hidden = true; const action = labPendingAction; labPendingAction = null; if (action) action(); });
  $("lab-confirm-no").addEventListener("click", () => { $("lab-confirm").hidden = true; labPendingAction = null; });
}
