"use strict";

const queueContent = document.getElementById("queue-content");
const stagingContent = document.getElementById("staging-content");
const executingContent = document.getElementById("executing-content");
const attentionContent = document.getElementById("attention-content");
const historyContent = document.getElementById("history-content");
const selected = new Set();
const explicitSelected = new Set();
const heldSelected = new Set();
const expanded = new Set();

let state = null;
let etag = null;
let pollTimer = null;
let historyPage = 1;
let historyPages = 1;
let lastHistoryRevision = -1;
let sessionExpired = false;
let cancellationBusy = false;
let focusedOperationId = null;
let detailRequestId = 0;
let todayExecutionCount = 0;
let lastTodayExecutionRevision = -1;
const cancelScopeDialog = document.getElementById("cancel-scope-dialog");

const operationLabels = {
  create_issue: "创建事务",
  clone_issue: "克隆事务",
  change_type: "修改事务类型",
  transition_issue: "流转状态",
  update_field: "更新字段",
  update_custom_field: "更新自定义字段",
  add_attachment: "上传附件",
  add_comment: "添加评论",
  add_worklog: "添加工作日志",
  add_issuelink: "关联事务",
};
const fieldLabels = {
  summary: "摘要",
  description: "描述",
  project_key: "项目",
  issue_type_id: "事务类型",
  priority: "优先级",
  assignee: "经办人",
  reporter: "报告人",
  labels: "标签",
  duedate: "到期日期",
  fixVersions: "修复版本",
  components: "组件",
  environment: "环境",
  issuetype: "事务类型",
  status: "状态",
};

const sessionExpiredMessage =
  "审核会话已失效。页面已停止刷新并禁用审核操作，请重新调用 jira_show_queue。";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function button(text, handler, className) {
  const node = el("button", className, text);
  node.type = "button";
  node.disabled = sessionExpired || (cancellationBusy && node.classList.contains("queue-cancel"));
  node.addEventListener("click", handler);
  return node;
}

function message(text) {
  document.getElementById("queue-message").textContent = sessionExpired
    ? sessionExpiredMessage
    : text || "";
}

function confirmAction(title, description, items, confirmLabel, danger = false) {
  const dialog = document.getElementById("action-confirm-dialog");
  const confirmButton = document.getElementById("action-confirm-accept");
  const cancelButton = document.getElementById("action-confirm-cancel");
  const list = document.getElementById("action-confirm-list");
  document.getElementById("action-confirm-title").textContent = title;
  document.getElementById("action-confirm-message").textContent = description;
  confirmButton.textContent = confirmLabel;
  confirmButton.className = danger ? "danger" : "primary";
  list.replaceChildren(...items.map((item) => el("li", null, item)));
  return new Promise((resolve) => {
    let settled = false;
    function finish(accepted) {
      if (settled) return;
      settled = true;
      confirmButton.removeEventListener("click", accept);
      cancelButton.removeEventListener("click", cancel);
      dialog.removeEventListener("cancel", onEscape);
      resolve(accepted && !sessionExpired);
    }
    function accept() {
      dialog.close("confirmed");
    }
    function cancel() {
      dialog.close("cancelled");
    }
    function onEscape(event) {
      event.preventDefault();
      cancel();
    }
    confirmButton.addEventListener("click", accept);
    cancelButton.addEventListener("click", cancel);
    dialog.addEventListener("cancel", onEscape);
    dialog.addEventListener(
      "close",
      () => finish(dialog.returnValue === "confirmed"),
      { once: true },
    );
    dialog.returnValue = "cancelled";
    dialog.showModal();
    cancelButton.focus();
  });
}

function requestResolutionInfo(title, description, fields) {
  const dialog = document.getElementById("resolution-dialog");
  const form = document.getElementById("resolution-form");
  const fieldContainer = document.getElementById("resolution-fields");
  const error = document.getElementById("resolution-error");
  const cancelButton = document.getElementById("resolution-cancel");
  form.noValidate = true;
  document.getElementById("resolution-title").textContent = title;
  document.getElementById("resolution-description").textContent = description;
  error.textContent = "";
  fieldContainer.replaceChildren(
    ...fields.map(({name, label, placeholder}) => {
      const wrapper = el("label", null, label);
      const input = el("input");
      input.name = name;
      input.id = `resolution-${name}`;
      input.type = "text";
      input.required = true;
      input.maxLength = name === "filename" ? 1024 : 255;
      input.placeholder = placeholder;
      wrapper.append(input);
      return wrapper;
    }),
  );
  return new Promise((resolve) => {
    let settled = false;
    function finish(value) {
      if (settled) return;
      settled = true;
      form.removeEventListener("submit", submit);
      cancelButton.removeEventListener("click", cancel);
      dialog.removeEventListener("cancel", onEscape);
      resolve(value && !sessionExpired ? value : null);
    }
    function submit(event) {
      event.preventDefault();
      const values = Object.fromEntries(
        fields.map(({name}) => [
          name,
          form.elements.namedItem(name).value.trim(),
        ]),
      );
      if (Object.values(values).some((value) => !value)) {
        error.textContent = "请填写所有必填信息。";
        return;
      }
      if (values.issue_key && !/^[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*$/.test(values.issue_key)) {
        error.textContent = "请输入有效的 Jira 事务 Key，例如 PROJ-123。";
        return;
      }
      dialog.close("submitted");
      finish(values);
    }
    function cancel() {
      dialog.close("cancelled");
      finish(null);
    }
    function onEscape(event) {
      event.preventDefault();
      cancel();
    }
    form.addEventListener("submit", submit);
    cancelButton.addEventListener("click", cancel);
    dialog.addEventListener("cancel", onEscape);
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "submitted") finish(null);
    }, { once: true });
    dialog.showModal();
    fieldContainer.querySelector("input")?.focus();
  });
}

function expireSession() {
  if (sessionExpired) return;
  sessionExpired = true;
  if (cancelScopeDialog.open) cancelScopeDialog.close("back");
  ["action-confirm-dialog", "resolution-dialog"].forEach((id) => {
    const dialog = document.getElementById(id);
    if (dialog.open) dialog.close("cancelled");
  });
  clearTimeout(pollTimer);
  document.querySelectorAll("button,input,select").forEach((control) => {
    control.disabled = true;
  });
  message(sessionExpiredMessage);
}

async function reviewFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 403) expireSession();
  return response;
}

function allQueueOperations() {
  if (!state) return [];
  return [
    ...state.queue.groups.flatMap((group) => group.operations),
    ...state.queue.standalone,
  ];
}

function queueOperationMap() {
  return new Map(allQueueOperations().map((item) => [item.operation_id, item]));
}

function dependencyIds(operation) {
  return Object.keys(operation.dependency_states || {});
}

function recomputeSelected() {
  const byId = queueOperationMap();
  selected.clear();
  const stack = [...explicitSelected];
  while (stack.length) {
    const operationId = stack.pop();
    const operation = byId.get(operationId);
    if (
      !operation ||
      operation.submittable === false ||
      selected.has(operationId)
    )
      continue;
    selected.add(operationId);
    dependencyIds(operation).forEach((dependencyId) => {
      if (byId.has(dependencyId)) stack.push(dependencyId);
    });
  }
}

function setExplicitSelection(operationId, checked) {
  if (checked) {
    explicitSelected.add(operationId);
  } else {
    explicitSelected.delete(operationId);
    const removed = new Set([operationId]);
    let changed = true;
    while (changed) {
      changed = false;
      allQueueOperations().forEach((operation) => {
        if (
          explicitSelected.has(operation.operation_id) &&
          dependencyIds(operation).some((id) => removed.has(id))
        ) {
          explicitSelected.delete(operation.operation_id);
          removed.add(operation.operation_id);
          changed = true;
        }
      });
    }
  }
  recomputeSelected();
  renderQueue();
}

function operationCard(operation, controls = []) {
  const card = el(
    "div",
    `operation risk-${operation.submission_risk_level || operation.risk_level}`,
  );
  card.dataset.operationId = operation.operation_id;
  const head = el("div", "operation-head");
  const title = el("div");
  const targets = [operation.issue_key || "无 issue key"];
  if (operation.target_issue_key) targets.push(`目标 ${operation.target_issue_key}`);
  const description =
    operation.operation_type === "transition_issue"
      ? `${operation.issue_key} 状态变更`
      : operation.description;
  title.append(
    el("div", "operation-title", description),
    el(
      "div",
      "meta",
      `${operation.operation_type} · ${operation.operation_id} · ${targets.join(" · ")} · ${operation.submission_risk_level || operation.risk_level} risk`,
    ),
  );
  const dependencies = dependencyIds(operation);
  if (dependencies.length) {
    const labels = dependencies.map(
      (id) => `${id}: ${operation.dependency_states?.[id] || "unknown"}`,
    );
    title.append(el("div", "meta dependency", `operation 依赖：${labels.join("；")}`));
  }
  if (operation.submission_block_reason) {
    title.append(
      el("div", "meta dependency-error", operation.submission_block_reason),
    );
  }
  head.append(title);
  if (controls.length) {
    const area = el("div", "operation-controls");
    controls.forEach((control) => area.append(control));
    head.append(area);
  }
  card.append(head);
  const payload = operation.payload || {};
  for (const key of ["issue_type_id", "target_issue_type_id", "transition_id"]) {
    if (payload[key]) card.append(el("div", "meta", `${key}：${payload[key]}`));
  }
  if (operation.operation_type === "clone_issue") {
    card.append(el("div", "meta", `克隆覆盖字段：${Object.keys(payload.field_overrides || {}).join("、") || "无"}`));
  }
  if (operation.operation_type === "add_worklog") {
    const mode = payload.adjust_estimate || "auto";
    const impact = {auto: "自动减少剩余估算", leave: "保持剩余估算", new: "重设剩余估算", manual: "按指定量减少剩余估算"};
    card.append(el("div", "meta", `估算影响：${impact[mode] || mode} ${payload.new_estimate || payload.reduce_by || ""}`));
  }

  const details = el("details");
  details.open = expanded.has(operation.operation_id);
  details.addEventListener("toggle", () => {
    if (details.open) expanded.add(operation.operation_id);
    else expanded.delete(operation.operation_id);
  });
  details.append(el("summary", null, "完整参数"));
  const pre = el("pre");
  pre.textContent = JSON.stringify(
    {
      issue_key: operation.issue_key,
      target_issue_key: operation.target_issue_key,
      ...operation.payload,
    },
    null,
    2,
  );
  details.append(pre);
  card.append(details);
  return card;
}

function queueCancelButton(operation) {
  return button(
    "取消此项",
    () => cancelOperations([operation.operation_id], state.revisions.queue),
    "danger queue-cancel",
  );
}

function operationRisk(operation) {
  return operation.submission_risk_level || operation.risk_level;
}

function proposedFields(operation) {
  const payload = operation.payload || {};
  if (operation.operation_type === "update_field") return payload.fields || {};
  if (operation.operation_type === "update_custom_field") return payload.custom_fields || {};
  if (operation.operation_type === "transition_issue") return payload.fields || {};
  if (operation.operation_type === "change_type") {
    return {
      issuetype: {id: payload.target_issue_type_id},
      ...(payload.fields_mapping || {}),
    };
  }
  if (operation.operation_type === "create_issue") {
    return Object.fromEntries(
      Object.entries({
        project_key: payload.project_key,
        issue_type_id: payload.issue_type_id,
        summary: payload.summary,
        description: payload.description,
        priority: payload.priority,
        assignee: payload.assignee,
        ...(payload.extra_fields || {}),
      }).filter(([, value]) => value !== null && value !== undefined && value !== ""),
    );
  }
  if (operation.operation_type === "clone_issue") {
    return {
      ...(payload.summary ? {summary: payload.summary} : {}),
      ...(payload.field_overrides || {}),
    };
  }
  return {};
}

function displayFieldName(fieldId) {
  return fieldLabels[fieldId] || (
    fieldId.startsWith("customfield_") ? `自定义字段 · ${fieldId}` : fieldId
  );
}

function displayFieldValue(value) {
  if (value === null) return "未设置";
  if (value === undefined) return "Jira 未返回字段值";
  if (Array.isArray(value)) {
    return value.map(displayFieldValue).join("、") || "未设置";
  }
  if (typeof value === "object") {
    for (const key of ["displayName", "name", "value", "key"]) {
      if (value[key] !== undefined && value[key] !== null) {
        return String(value[key]);
      }
    }
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function previewValue(field) {
  const current = field.current_value;
  const type = field.schema?.type;
  if (type === "number" || type === "integer") {
    const number = Number(current);
    return Number.isFinite(number) ? number + 1 : 1;
  }
  if (type === "date") {
    const date = typeof current === "string"
      ? new Date(`${current}T00:00:00Z`)
      : new Date();
    if (!Number.isNaN(date.valueOf())) {
      date.setUTCDate(date.getUTCDate() + 1);
      return date.toISOString().slice(0, 10);
    }
  }
  if (type === "array" && field.schema?.items === "string") {
    const values = Array.isArray(current) ? current.map(String) : [];
    let label = "preview-label";
    while (values.includes(label)) label = `new-${label}`;
    return [...values, label];
  }
  if (type === "string") {
    return current
      ? `${displayFieldValue(current)}（预览修改）`
      : "示例文本（仅预览）";
  }
  return `示例${field.name || field.field_id}（仅预览）`;
}

function renderFieldPreview(data) {
  const results = document.getElementById("field-preview-results");
  results.replaceChildren();
  const table = el("div", "diff-table");
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Jira 字段变更前后对比");
  const header = el("div", "diff-header");
  header.setAttribute("role", "row");
  ["字段", "原值", "修改后（示例）"].forEach((label) => {
    const cell = el("span", null, label);
    cell.setAttribute("role", "columnheader");
    header.append(cell);
  });
  table.append(header);
  for (const field of data.fields || []) {
    const row = el("div", "diff-row");
    row.setAttribute("role", "row");
    const name = el("div", "diff-field");
    name.setAttribute("role", "rowheader");
    name.append(
      el("span", null, field.name || displayFieldName(field.field_id)),
      el("span", "diff-status changed", "预览修改"),
    );
    const current = el(
      "div",
      "diff-value",
      field.has_current_value ? displayFieldValue(field.current_value) : "未设置",
    );
    current.setAttribute("role", "cell");
    const proposed = el("div", "diff-value after", displayFieldValue(previewValue(field)));
    proposed.setAttribute("role", "cell");
    row.append(name, current, proposed);
    table.append(row);
  }
  if (!data.fields?.length) {
    results.append(el("p", null, "此事务当前没有可编辑字段。"));
  } else {
    results.append(table);
  }
  results.hidden = false;
}

async function loadFieldPreview(issueKey) {
  const messageNode = document.getElementById("field-preview-message");
  const results = document.getElementById("field-preview-results");
  const submit = document.getElementById("field-preview-load");
  messageNode.textContent = "正在读取 Jira 可编辑字段…";
  results.hidden = true;
  submit.disabled = true;
  try {
    const response = await reviewFetch("/api/issue-preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({issue_key: issueKey}),
    });
    const data = await response.json();
    if (!response.ok) {
      messageNode.textContent = data.error || "读取 Jira 可编辑字段失败。";
      return;
    }
    renderFieldPreview(data);
    messageNode.textContent = `${data.key}：共 ${data.fields.length} 个可编辑字段。以下修改值仅为演示，不会提交。`;
  } catch {
    messageNode.textContent = "读取 Jira 可编辑字段失败，请检查本地网络和 Jira 连接。";
  } finally {
    submit.disabled = sessionExpired;
  }
}

function addDetailSection(container, title) {
  const section = el("section", "detail-section");
  section.append(el("h3", null, title));
  const fullParameters = container.querySelector("details");
  if (fullParameters) fullParameters.before(section);
  else container.append(section);
  return section;
}

function renderDiffTable(
  container,
  operation,
  currentValues,
  readError,
  noCurrentValue = false,
) {
  const fields = proposedFields(operation);
  const entries = Object.entries(fields);
  if (!entries.length) return;
  container.querySelector(".diff-section")?.remove();
  const section = addDetailSection(container, "变更对比");
  section.classList.add("diff-section");
  const table = el("div", "diff-table");
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Jira 字段变更前后对比");
  const header = el("div", "diff-header");
  header.setAttribute("role", "row");
  ["字段", "当前值", "修改后"].forEach((label) => {
    const cell = el("span", null, label);
    cell.setAttribute("role", "columnheader");
    header.append(cell);
  });
  table.append(header);
  entries.forEach(([fieldId, value]) => {
    const row = el("div", "diff-row");
    row.setAttribute("role", "row");
    const field = el("div", "diff-field");
    field.setAttribute("role", "rowheader");
    field.append(el("span", null, displayFieldName(fieldId)));
    let changeLabel = "修改";
    let changeClass = "diff-status changed";
    const hasCurrentValue =
      currentValues !== null && Object.hasOwn(currentValues, fieldId);
    if (noCurrentValue) {
      changeLabel = "新增";
      changeClass = "diff-status";
    } else if (readError || (currentValues !== null && !hasCurrentValue)) {
      changeLabel = "未知";
      changeClass = "diff-status unknown";
    } else if (
      hasCurrentValue &&
      (currentValues[fieldId] === null ||
        currentValues[fieldId] === "" ||
        (Array.isArray(currentValues[fieldId]) && !currentValues[fieldId].length))
    ) {
      changeLabel = "新增";
      changeClass = "diff-status";
    }
    field.append(el("span", changeClass, changeLabel));
    const current = el("div", "diff-value loading");
    if (noCurrentValue) {
      current.className = "diff-value empty";
      current.textContent = "尚无 Jira 当前值";
    } else if (readError) {
      current.className = "diff-value error";
      current.textContent = "读取失败";
    } else if (currentValues === null) {
      current.textContent = "正在读取 Jira 当前值…";
    } else if (!Object.hasOwn(currentValues, fieldId)) {
      current.className = "diff-value empty";
      current.textContent = "Jira 未返回字段值";
    } else {
      current.className = "diff-value";
      current.textContent = displayFieldValue(currentValues[fieldId]);
    }
    current.setAttribute("role", "cell");
    const after = el("div", "diff-value after", displayFieldValue(value));
    after.setAttribute("role", "cell");
    row.append(field, current, after);
    table.append(row);
  });
  section.append(table);
  if (readError) {
    section.append(el("div", `detail-notice${noCurrentValue ? "" : " error"}`, readError));
  } else {
    section.append(
      el(
        "div",
        "detail-notice",
        "当前值为页面查看时从 Jira 读取；其他用户后续的修改可能导致执行时值发生变化。",
      ),
    );
  }
}

function renderTransitionPreview(container, preview, error = null) {
  container.querySelector(".transition-section")?.remove();
  const section = addDetailSection(container, "变更对比");
  section.classList.add("transition-section");
  if (error) {
    section.append(el("div", "detail-notice error", error));
    return;
  }
  const transition = preview?.transition || {};
  const table = el("div", "diff-table");
  table.setAttribute("role", "table");
  table.setAttribute("aria-label", "Jira 字段变更前后对比");
  const header = el("div", "diff-header");
  header.setAttribute("role", "row");
  ["字段", "原值", "修改后"].forEach((label) => {
    const cell = el("span", null, label);
    cell.setAttribute("role", "columnheader");
    header.append(cell);
  });
  table.append(header);
  const row = el("div", "diff-row");
  row.setAttribute("role", "row");
  const field = el("div", "diff-field");
  field.setAttribute("role", "rowheader");
  field.textContent = "状态";
  const current = el(
    "div",
    "diff-value",
    preview ? preview.current_status || "Jira 未返回字段值" : "正在读取 Jira 当前值…",
  );
  current.setAttribute("role", "cell");
  const after = el(
    "div",
    "diff-value after",
    preview ? transition.target_status || "Jira 未提供目标状态" : "正在读取流转目标…",
  );
  after.setAttribute("role", "cell");
  row.append(field, current, after);
  table.append(row);
  section.append(table);
}

async function loadIssueTransition(operation, requestId, container) {
  const response = await reviewFetch("/api/issue-transition", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      issue_key: operation.issue_key,
      transition_id: String(operation.payload.transition_id),
    }),
  });
  if (sessionExpired || requestId !== detailRequestId) return;
  const data = await response.json();
  if (!response.ok) {
    renderTransitionPreview(
      container,
      null,
      data.error || "读取 Jira 状态流转信息失败。",
    );
    return;
  }
  renderTransitionPreview(container, data);
}

async function loadIssueFields(operation, requestId, container) {
  const issueKey =
    operation.operation_type === "clone_issue" ? null : operation.issue_key;
  if (!/^[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*$/.test(issueKey || "")) {
    renderDiffTable(
      container,
      operation,
      {},
      "这是待创建事务，提交后才会创建这些字段。",
      true,
    );
    return;
  }
  const fields = Object.keys(proposedFields(operation));
  const response = await reviewFetch("/api/issue-fields", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ issue_key: issueKey, fields }),
  });
  if (sessionExpired || requestId !== detailRequestId) return;
  const data = await response.json();
  if (!response.ok) {
    renderDiffTable(
      container,
      operation,
      {},
      data.error || "读取 Jira 当前值失败，请检查连接后重试。",
    );
    return;
  }
  renderDiffTable(container, operation, data.fields || {}, null);
}

function renderOperationDetail(operation) {
  const requestId = ++detailRequestId;
  const empty = document.getElementById("detail-empty");
  const content = document.getElementById("detail-content");
  content.replaceChildren();
  if (!operation) {
    empty.hidden = false;
    content.hidden = true;
    return;
  }
  empty.hidden = true;
  content.hidden = false;
  const title = el("div", "detail-eyebrow", "操作详情");
  content.append(
    title,
    el(
      "h2",
      "detail-title",
      operation.operation_type === "transition_issue"
        ? `${operation.issue_key} 状态变更`
        : operation.description,
    ),
    el(
      "p",
      "detail-context",
      `${operation.operation_type === "clone_issue" ? `源事务 ${operation.issue_key} · 新事务` : operation.issue_key || "新事务"}${operation.target_issue_key ? ` · 目标 ${operation.target_issue_key}` : ""}`,
    ),
  );
  const tags = el("div", "detail-tags");
  const risk = operationRisk(operation);
  tags.append(
    el("span", `detail-tag ${risk === "high" ? "" : "blue"}`, `${{low: "低风险", medium: "中风险", high: "高风险"}[risk] || risk}`),
    el("span", "detail-tag", operationLabels[operation.operation_type] || operation.operation_type),
    el("span", "detail-tag", operation.submittable === false ? "受阻" : "待审核"),
  );
  content.append(tags);

  const summary = addDetailSection(content, "操作信息");
  const meta = el("div", "detail-meta");
  [
    [operation.operation_type === "clone_issue" ? "源事务" : "事务", operation.issue_key || "尚未创建"],
    ["操作 ID", operation.operation_id],
    ["提交来源", operation.source || "未知"],
  ].forEach(([label, value]) => {
    const row = el("div", "detail-meta-row");
    row.append(el("span", "detail-meta-label", label), el("span", "detail-meta-value", value));
    meta.append(row);
  });
  summary.append(meta);

  const fields = proposedFields(operation);
  if (operation.operation_type === "transition_issue") {
    renderTransitionPreview(content, null);
    loadIssueTransition(operation, requestId, content).catch(() => {
      if (requestId === detailRequestId) {
        renderTransitionPreview(
          content,
          null,
          "读取 Jira 状态流转信息失败，请检查本地网络和 Jira 连接。",
        );
      }
    });
  }
  if (Object.keys(fields).length) {
    renderDiffTable(content, operation, null, null);
    loadIssueFields(operation, requestId, content).catch(() => {
      if (requestId === detailRequestId) {
        renderDiffTable(
          content,
          operation,
          {},
          "读取 Jira 当前值失败，请检查本地网络和 Jira 连接。",
        );
      }
    });
  } else if (operation.operation_type === "transition_issue") {
    if (operation.payload?.comment) {
      const section = addDetailSection(content, "附带评论");
      section.append(el("pre", "change-preview", operation.payload.comment));
    }
  } else {
    const section = addDetailSection(content, "操作内容");
    const payload = operation.payload || {};
    if (operation.operation_type === "clone_issue") {
      section.append(
        el(
          "div",
          "meta",
          `克隆覆盖字段：${Object.keys(payload.field_overrides || {}).join("、") || "无"}`,
        ),
      );
    }
    if (operation.operation_type === "add_worklog") {
      const mode = payload.adjust_estimate || "auto";
      const impact = {
        auto: "自动减少剩余估算",
        leave: "保持剩余估算",
        new: "重设剩余估算",
        manual: "按指定量减少剩余估算",
      };
      section.append(
        el(
          "div",
          "meta",
          `估算影响：${impact[mode] || mode} ${payload.new_estimate || payload.reduce_by || ""}`,
        ),
      );
    }
    for (const key of ["issue_type_id", "target_issue_type_id", "transition_id"]) {
      if (payload[key] && !(key === "transition_id" && operation.operation_type === "transition_issue")) {
        section.append(el("div", "meta", `${key}：${payload[key]}`));
      }
    }
    const preview = payload.body || payload.comment || operation.description;
    section.append(el("pre", "change-preview", preview));
  }

  if (operation.submission_block_reason) {
    content.append(el("div", "detail-notice error", operation.submission_block_reason));
  } else if (dependencyIds(operation).length) {
    content.append(el("div", "detail-notice", `依赖操作：${dependencyIds(operation).join("、")}`));
  }
  const details = el("details");
  details.append(el("summary", null, "查看完整参数"));
  const pre = el("pre");
  pre.textContent = JSON.stringify(
    {
      issue_key: operation.issue_key,
      target_issue_key: operation.target_issue_key,
      ...operation.payload,
    },
    null,
    2,
  );
  details.append(pre);
  content.append(details);
}

function matchesQueueFilters(operation) {
  const query = document.getElementById("queue-search").value.trim().toLowerCase();
  const risk = document.getElementById("risk-filter").value;
  const type = document.getElementById("type-filter").value;
  const searchable = `${operation.description} ${operation.issue_key || ""} ${operation.target_issue_key || ""}`.toLowerCase();
  return (!query || searchable.includes(query))
    && (!risk || operationRisk(operation) === risk)
    && (!type || operation.operation_type === type);
}

function updateOverview() {
  if (!state) return;
  const queued = allQueueOperations().length;
  const activeExecution = state.execution.executing.length + state.execution.staged.length;
  const attention = state.execution.indeterminate.length + state.execution.held.length;
  document.getElementById("metric-queue").textContent = String(queued);
  document.getElementById("metric-executing").textContent = String(state.execution.executing.length);
  document.getElementById("metric-attention").textContent = String(attention);
  document.getElementById("metric-today").textContent = String(todayExecutionCount);
  document.getElementById("tab-count-queue").textContent = String(queued);
  document.getElementById("tab-count-executing").textContent = String(activeExecution);
  document.getElementById("tab-count-attention").textContent = String(attention);
  document.getElementById("selected-count").textContent = String(selected.size);
  document.getElementById("selected-label").textContent = `${selected.size} 项`;
  document.getElementById("selection-hint").textContent =
    `${Math.max(0, selected.size - explicitSelected.size)} 项由依赖关系自动纳入`;
  document.getElementById("submit-selected").disabled = sessionExpired || selected.size === 0;
  document.getElementById("cancel-selected").disabled = sessionExpired || explicitSelected.size === 0;
}

function renderQueue() {
  queueContent.replaceChildren();
  const available = new Set(
    allQueueOperations().map((item) => item.operation_id),
  );
  [...selected].forEach((id) => {
    if (!available.has(id)) selected.delete(id);
  });
  [...explicitSelected].forEach((id) => {
    if (!available.has(id)) explicitSelected.delete(id);
  });
  recomputeSelected();

  const types = [...new Set(allQueueOperations().map((item) => item.operation_type))];
  const typeFilter = document.getElementById("type-filter");
  const previousType = typeFilter.value;
  typeFilter.replaceChildren(el("option", null, "所有操作"));
  typeFilter.firstElementChild.value = "";
  types.forEach((type) => {
    const option = el("option", null, operationLabels[type] || type);
    option.value = type;
    typeFilter.append(option);
  });
  if (types.includes(previousType)) typeFilter.value = previousType;

  const visible = [];
  state.queue.groups.forEach((group) => {
    const operations = group.operations.filter(matchesQueueFilters);
    if (!operations.length) return;
    const box = el("div", group.kind === "pending_issue" ? "issue-group planned-group" : "issue-group");
    const heading =
      group.kind === "pending_issue"
        ? `待创建事务 · ${group.producer_state || "等待创建"} · ${operations.length} 项`
        : `${group.group_key} · ${operations.length} 项`;
    box.append(el("h3", null, heading));
    operations.forEach((operation) => {
      visible.push(operation);
      const row = el("div", `queue-item${operation.operation_id === focusedOperationId ? " is-focused" : ""}`);
      row.dataset.operationId = operation.operation_id;
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected.has(operation.operation_id);
      checkbox.disabled = sessionExpired || operation.submittable === false;
      checkbox.title = selected.has(operation.operation_id) && !explicitSelected.has(operation.operation_id)
        ? "因依赖关系自动勾选"
        : "";
      checkbox.addEventListener("change", () =>
        setExplicitSelection(operation.operation_id, checkbox.checked),
      );
      const main = el("div", "queue-item-main");
      main.append(
        el("div", "queue-item-title", operation.description),
        el("div", "queue-item-meta"),
      );
      const metadata = main.lastElementChild;
      const risk = operationRisk(operation);
      metadata.append(
        el("span", `risk risk-${risk}`, `${{low: "低风险", medium: "中风险", high: "高风险"}[risk] || risk}`),
        el("span", null, operationLabels[operation.operation_type] || operation.operation_type),
      );
      if (dependencyIds(operation).length) {
        const dependencies = dependencyIds(operation).map(
          (id) => `${id}: ${operation.dependency_states?.[id] || "unknown"}`,
        );
        metadata.append(el("span", "dependency", `依赖：${dependencies.join("；")}`));
      }
      if (operation.submission_block_reason) {
        metadata.append(el("span", "dependency-error", operation.submission_block_reason));
      }
      row.append(checkbox, main, el("span", "queue-item-arrow", "›"));
      if (operation.submittable === false) {
        row.append(queueCancelButton(operation));
      }
      row.addEventListener("click", (event) => {
        if (event.target.closest("button,input")) return;
        focusedOperationId = operation.operation_id;
        renderQueue();
      });
      box.append(row);
    });
    queueContent.append(box);
  });

  const standalone = state.queue.standalone.filter(matchesQueueFilters);
  if (standalone.length) {
    const box = el("div", "standalone");
    box.append(el("h3", null, `其他事务 · ${standalone.length} 项`));
    standalone.forEach((operation) => {
      visible.push(operation);
      const row = el("div", `queue-item${operation.operation_id === focusedOperationId ? " is-focused" : ""}`);
      row.dataset.operationId = operation.operation_id;
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.checked = selected.has(operation.operation_id);
      checkbox.disabled = sessionExpired || operation.submittable === false;
      checkbox.addEventListener("change", () =>
        setExplicitSelection(operation.operation_id, checkbox.checked),
      );
      const main = el("div", "queue-item-main");
      const metadata = el("div", "queue-item-meta");
      const risk = operationRisk(operation);
      main.append(
        el("div", "queue-item-title", operation.description),
        metadata,
      );
      metadata.append(
        el("span", `risk risk-${risk}`, `${{low: "低风险", medium: "中风险", high: "高风险"}[risk] || risk}`),
        el("span", null, operationLabels[operation.operation_type] || operation.operation_type),
      );
      if (dependencyIds(operation).length) {
        const dependencies = dependencyIds(operation).map(
          (id) => `${id}: ${operation.dependency_states?.[id] || "unknown"}`,
        );
        metadata.append(el("span", "dependency", `依赖：${dependencies.join("；")}`));
      }
      row.append(checkbox, main, el("span", "queue-item-arrow", "›"));
      if (operation.submittable === false) row.append(queueCancelButton(operation));
      row.addEventListener("click", (event) => {
        if (event.target.closest("button,input")) return;
        focusedOperationId = operation.operation_id;
        renderQueue();
      });
      box.append(row);
    });
    queueContent.append(box);
  }
  if (!visible.length) {
    queueContent.append(el("div", "empty", allQueueOperations().length ? "没有符合筛选条件的操作" : "当前队列为空"));
  }
  if (!visible.some((operation) => operation.operation_id === focusedOperationId)) {
    focusedOperationId = selected.values().next().value || visible[0]?.operation_id || null;
  }
  const focused = queueOperationMap().get(focusedOperationId);
  renderOperationDetail(focused || null);
  updateOverview();
}

function renderExecution() {
  stagingContent.replaceChildren();
  attentionContent.replaceChildren();
  const heldIds = new Set(
    state.execution.held.map((item) => item.operation_id),
  );
  [...heldSelected].forEach((id) => {
    if (!heldIds.has(id)) heldSelected.delete(id);
  });
  if (state.execution.held.length) {
    const bulk = el("div", "actions");
    bulk.append(
      button("恢复勾选 held", () => resolveHeld([...heldSelected], "resume")),
      button("退回勾选 held", () => resolveHeld([...heldSelected], "requeue")),
      button(
        "放弃勾选 held",
        () => resolveHeld([...heldSelected], "abandon"),
        "danger",
      ),
    );
    attentionContent.append(bulk);
  }
  const staged = [
    ...state.execution.staged,
  ];
  staged.forEach((operation) => {
    const controls = [];
    const card = operationCard(operation, controls);
    card.prepend(el("span", `status ${operation.state}`, operation.state === "staged" ? "等待执行" : operation.state));
    stagingContent.append(card);
  });
  if (!staged.length) {
    stagingContent.append(el("div", "empty", "暂无等待执行的操作"));
  }

  const attention = [
    ...state.execution.held,
    ...state.execution.indeterminate,
  ];
  attention.forEach((operation) => {
    const controls = [];
    if (operation.state === "indeterminate") {
      controls.push(
        button("已生效", () => resolveIndeterminate(operation, "succeeded")),
        button("未生效", () => resolveIndeterminate(operation, "not_applied")),
        button(
          "放弃",
          () => resolveIndeterminate(operation, "abandoned"),
          "danger",
        ),
      );
    } else if (operation.state === "held") {
      const check = el("input");
      check.type = "checkbox";
      check.checked = heldSelected.has(operation.operation_id);
      check.disabled = sessionExpired;
      check.addEventListener("change", () => {
        if (check.checked) heldSelected.add(operation.operation_id);
        else heldSelected.delete(operation.operation_id);
      });
      controls.push(
        check,
        button("恢复", () => resolveHeld([operation.operation_id], "resume")),
        button("退回队列", () => resolveHeld([operation.operation_id], "requeue")),
        button(
          "放弃",
          () => resolveHeld([operation.operation_id], "abandon"),
          "danger",
        ),
      );
    }
    const card = operationCard(operation, controls);
    card.prepend(el("span", `status ${operation.state}`, operation.state));
    attentionContent.append(card);
  });
  if (!attention.length) {
    attentionContent.append(el("div", "empty", "暂无需要人工处理的操作"));
  }

  executingContent.replaceChildren();
  state.execution.executing.forEach((operation) => {
    const card = operationCard(operation);
    card.prepend(el("span", "status", "executing"));
    executingContent.append(card);
  });
  if (!state.execution.executing.length) {
    executingContent.append(
      el("div", "empty", "当前没有 Jira 写操作执行中"),
    );
  }
  updateOverview();
}

async function resolveIndeterminate(operation, resolution) {
  let highRiskConfirmed = false;
  if (
    resolution === "not_applied" &&
    operation.submission_risk_level === "high"
  ) {
    highRiskConfirmed = await confirmAction(
      "确认高风险操作未生效",
      "只有在 Jira 上确认该操作确实未生效后，才能将它重新入队。",
      [operation.description],
      "确认未生效",
      true,
    );
    if (!highRiskConfirmed) return;
  }
  let newIssueKey = null;
  let attachment = null;
  if (
    resolution === "succeeded" &&
    ["create_issue", "clone_issue"].includes(operation.operation_type)
  ) {
    const values = await requestResolutionInfo(
      "确认新事务 Key",
      "请核实 Jira 已成功创建事务，并填写实际生成的 Key。",
      [{name: "issue_key", label: "Jira 事务 Key", placeholder: "例如 PROJ-123"}],
    );
    if (!values) return;
    newIssueKey = values.issue_key;
  }
  if (
    resolution === "succeeded" &&
    operation.operation_type === "add_attachment"
  ) {
    const values = await requestResolutionInfo(
      "确认附件身份",
      "请从 Jira 中核实附件上传结果，再填写附件 ID 和 Jira 保存的最终文件名。",
      [
        {name: "attachment_id", label: "Jira 附件 ID", placeholder: "附件 ID"},
        {name: "filename", label: "最终文件名", placeholder: "例如 evidence.pdf"},
      ],
    );
    if (!values) return;
    attachment = { id: values.attachment_id, filename: values.filename };
  }
  await mutate("/api/execution/indeterminate/resolve", {
    operation_id: operation.operation_id,
    resolution,
    high_risk_confirmed: highRiskConfirmed,
    new_issue_key: newIssueKey,
    attachment,
  });
}

async function resolveHeld(ids, action) {
  if (!ids.length) {
    message("请先勾选 held operation");
    return;
  }
  if (action === "abandon") {
    const byId = new Map(
      state.execution.held.map((item) => [item.operation_id, item]),
    );
    const confirmed = await confirmAction(
      "确认放弃暂存操作",
      "放弃后这些操作不会再提交到 Jira。",
      ids.map((id) => byId.get(id)?.description || id),
      "确认放弃",
      true,
    );
    if (!confirmed) return;
  }
  await mutate("/api/execution/held/resolve", { operation_ids: ids, action });
}

async function mutate(url, body) {
  if (sessionExpired) return;
  const response = await reviewFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (sessionExpired) return;
  const data = await response.json();
  if (!response.ok) {
    message(data.error || "操作失败");
    return;
  }
  etag = null;
  await loadState(true);
}

async function submitSelected(highRiskConfirmed = false) {
  if (sessionExpired) return;
  const ids = [...selected];
  if (!ids.length) {
    message("请先勾选 operation");
    return;
  }
  const response = await reviewFetch("/api/queue/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_queue_revision: state.revisions.queue,
      operation_ids: ids,
      high_risk_confirmed: highRiskConfirmed,
    }),
  });
  if (sessionExpired) return;
  const data = await response.json();
  if (
    response.status === 409 &&
    data.code === "confirmation_required" &&
    !highRiskConfirmed
  ) {
    const byId = queueOperationMap();
    const highRiskOperations = ids
      .map((id) => byId.get(id))
      .filter((operation) => operation && operationRisk(operation) === "high");
    if (await confirmAction(
      "确认提交高风险操作",
      "以下操作可能产生重要或难以撤销的 Jira 变更。请逐项核对后授权。",
      highRiskOperations.map((operation) => operation.description),
      "确认提交高风险操作",
      true,
    )) {
      return submitSelected(true);
    }
    return;
  }
  if (!response.ok) {
    message(data.error || "提交失败");
    if (data.code === "revision_conflict") {
      etag = null;
      await loadState(true);
    }
    return;
  }
  ids.forEach((id) => {
    selected.delete(id);
    explicitSelected.delete(id);
  });
  message("已授权并移入暂存区；页面将持续显示实际执行结果。");
  etag = null;
  await loadState(true);
}

function setCancellationBusy(busy) {
  cancellationBusy = busy;
  document.querySelectorAll("#cancel-selected, .queue-cancel").forEach((control) => {
    control.disabled = sessionExpired || busy;
  });
}

function cancellationTarget(key) {
  if (!key) return "尚未生成 Jira Key";
  return /^op-[a-f0-9]{32}$/.test(key) ? `待创建操作 ${key}` : key;
}

function confirmCancellationScope(affected, missing, snapshot, requestedCount) {
  const list = document.getElementById("cancel-scope-list");
  const confirmButton = document.getElementById("cancel-scope-confirm");
  const backButton = document.getElementById("cancel-scope-back");
  const error = document.getElementById("cancel-scope-error");
  const added = new Set(missing);
  document.getElementById("cancel-scope-summary").textContent =
    `原先选择 ${requestedCount} 项，新增依赖 ${missing.length} 项，共 ${affected.length} 项。`;
  list.replaceChildren(...affected.map((id) => {
    const operation = snapshot.get(id);
    const item = el("li", `cancel-scope-item${added.has(id) ? " is-added" : ""}`);
    item.dataset.cancelOperationId = id;
    item.append(
      el("span", "cancel-scope-label", added.has(id) ? "新增依赖" : "原先选择"),
      el("div", "operation-title", operation.description),
      el("div", "meta", `操作 ID：${id}`),
      el("div", "meta", `操作类型：${operation.operation_type}`),
      el("div", "meta", `关联事务：${cancellationTarget(operation.issue_key)}`),
    );
    if (operation.target_issue_key) {
      item.append(el("div", "meta", `目标事务：${cancellationTarget(operation.target_issue_key)}`));
    }
    return item;
  }));
  const exceedsLimit = affected.length > 1000;
  error.textContent = exceedsLimit
    ? "完整取消范围超过单次 1000 项限制，无法一次取消；请返回并重新选择满足依赖范围的操作。"
    : "";
  confirmButton.textContent = `确认取消全部 ${affected.length} 项`;
  confirmButton.disabled = exceedsLimit;
  backButton.disabled = false;
  return new Promise((resolve) => {
    function accept() {
      if (sessionExpired || confirmButton.disabled) return;
      confirmButton.disabled = true;
      cancelScopeDialog.close("confirmed");
    }
    function decline() {
      cancelScopeDialog.close("back");
    }
    function escape(event) {
      event.preventDefault();
      decline();
    }
    function finish() {
      confirmButton.removeEventListener("click", accept);
      backButton.removeEventListener("click", decline);
      cancelScopeDialog.removeEventListener("cancel", escape);
      resolve(!sessionExpired && cancelScopeDialog.returnValue === "confirmed");
    }
    confirmButton.addEventListener("click", accept);
    backButton.addEventListener("click", decline);
    cancelScopeDialog.addEventListener("cancel", escape);
    cancelScopeDialog.addEventListener("close", finish, { once: true });
    cancelScopeDialog.returnValue = "back";
    cancelScopeDialog.showModal();
    backButton.focus();
  });
}

async function cancelSelected() {
  if (sessionExpired || cancellationBusy) return;
  const ids = [...explicitSelected];
  if (!ids.length) {
    message("请先勾选 operation");
    return;
  }
  const operations = queueOperationMap();
  const confirmed = await confirmAction(
    "确认取消待审核操作",
    "这些操作只会从本地审核队列撤回，不会写入 Jira。",
    ids.map((id) => operations.get(id)?.description || id),
    "确认取消所选",
    true,
  );
  if (!confirmed || sessionExpired) return;
  await cancelOperations(ids, state.revisions.queue);
}

async function cancelOperations(ids, expectedQueueRevision) {
  if (sessionExpired || cancellationBusy) return;
  const trigger = document.activeElement;
  const triggerOperationId = trigger?.closest("[data-operation-id]")?.dataset.operationId;
  const requestedIds = [...ids];
  // Copy only displayed immutable fields before the first asynchronous request.
  const snapshot = new Map(allQueueOperations().map((operation) => [
    operation.operation_id,
    {
      operation_type: operation.operation_type,
      description: operation.description,
      issue_key: operation.issue_key,
      target_issue_key: operation.target_issue_key,
    },
  ]));
  setCancellationBusy(true);
  async function send(operationIds) {
    const response = await reviewFetch("/api/queue/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_queue_revision: expectedQueueRevision,
        operation_ids: operationIds,
      }),
    });
    return { response, data: sessionExpired ? null : await response.json() };
  }
  try {
    let { response, data } = await send(requestedIds);
    if (sessionExpired) return;
    if (response.status === 409 && data.code === "dependency_scope_incomplete") {
      const affected = data.affected_operation_ids;
      const missing = data.missing_operation_ids;
      if (!Array.isArray(affected) || !affected.length || !Array.isArray(missing) ||
          affected.some((id) => !snapshot.has(id))) {
        message("取消范围的展示记录不完整，请刷新后重新选择并取消。");
        etag = null;
        await loadState(true);
        return;
      }
      const confirmed = await confirmCancellationScope(affected, missing, snapshot, requestedIds.length);
      if (!confirmed || sessionExpired) return;
      ({ response, data } = await send(affected));
      if (sessionExpired) return;
    }
    if (!response.ok) {
      message(data.error || "取消失败");
      if (data.code === "revision_conflict") {
        etag = null;
        await loadState(true);
      }
      return;
    }
    (data.cancelled_operation_ids || []).forEach((id) => {
      selected.delete(id);
      explicitSelected.delete(id);
    });
    message("已从本地审核队列取消所选 operation；未写入 Jira。");
    etag = null;
    await loadState(true);
  } catch (_error) {
    message("取消请求失败，请刷新确认队列状态后重试。");
  } finally {
    setCancellationBusy(false);
    if (!sessionExpired) {
      const replacement = triggerOperationId
        ? [...queueContent.querySelectorAll("[data-operation-id]")]
            .find((card) => card.dataset.operationId === triggerOperationId)
            ?.querySelector(".queue-cancel")
        : null;
      const focusTarget = trigger?.isConnected ? trigger : replacement;
      (focusTarget || document.getElementById("cancel-selected")).focus();
    }
  }
}

async function loadHistory(force = false) {
  if (!state || sessionExpired) return;
  if (!force && lastHistoryRevision === state.revisions.history) return;
  const date = document.getElementById("history-date").value;
  let query = `page=${historyPage}&page_size=50`;
  if (date) {
    const start = new Date(`${date}T00:00:00`);
    const end = new Date(start);
    end.setDate(end.getDate() + 1);
    query += `&start_at=${start.getTime()}&end_at=${end.getTime()}`;
  }
  const response = await reviewFetch(`/api/execution-history?${query}`);
  if (sessionExpired || !response.ok) return;
  const data = await response.json();
  lastHistoryRevision = state.revisions.history;
  historyPages = Math.max(1, Math.ceil(data.total / data.page_size));
  historyContent.replaceChildren();
  let currentDate = "";
  data.items.forEach((item) => {
    const localDate = new Date(item.started_at).toLocaleDateString();
    if (localDate !== currentDate) {
      currentDate = localDate;
      historyContent.append(el("h3", null, localDate));
    }
    const card = operationCard(item);
    card.prepend(
      el(
        "span",
        `status ${item.outcome}`,
        item.resolution
          ? `${item.outcome} · ${item.resolution}`
          : item.outcome === "warning" ? "成功（有异常）" : item.outcome,
      ),
    );
    card.append(el("div", "meta", new Date(item.started_at).toLocaleString()));
    if (item.summary) card.append(el("div", "meta", item.summary));
    if (item.new_issue_key) card.append(el("div", "meta", `已创建事务：${item.new_issue_key}`));
    const labels = {success: "成功", failed: "失败", unknown: "结果未知", unattempted: "未尝试"};
    for (const result of item.attachment_results) {
      const source = result.source_attachment_id ? `（源 ID ${result.source_attachment_id}）` : "";
      const target = result.attachment ? ` → ${result.attachment.filename}（ID ${result.attachment.id}）` : "";
      card.append(el("div", "meta", `附件处理：${result.requested_filename}${source}${target}：${labels[result.status]}${result.error ? `；${result.error}` : ""}`));
    }
    if (item.error) card.append(el("div", "meta", item.error));
    historyContent.append(card);
  });
  if (!data.items.length) {
    historyContent.append(el("div", "empty", "该日期没有执行记录"));
  }
  document.getElementById("history-page").textContent =
    `${historyPage} / ${historyPages}`;
  document.getElementById("history-prev").disabled = historyPage <= 1;
  document.getElementById("history-next").disabled = historyPage >= historyPages;
}

async function loadTodayCount() {
  if (lastTodayExecutionRevision === state.revisions.history) return;
  lastTodayExecutionRevision = state.revisions.history;
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  try {
    const response = await reviewFetch(
      `/api/execution-history?start_at=${start.getTime()}&end_at=${end.getTime()}&page=1&page_size=1`,
    );
    if (sessionExpired) return;
    if (!response.ok) throw new Error("today-history");
    const data = await response.json();
    todayExecutionCount = data.total;
    updateOverview();
  } catch (_error) {
    if (!sessionExpired) {
      lastTodayExecutionRevision = -1;
      document.getElementById("metric-today").textContent = "—";
      message("今日执行数读取失败");
    }
  }
}

async function loadState(force = false) {
  if (sessionExpired) return;
  clearTimeout(pollTimer);
  const headers = {};
  if (etag && !force) headers["If-None-Match"] = etag;
  try {
    const response = await reviewFetch("/api/state", { headers });
    if (sessionExpired) return;
    if (response.status !== 304) {
      if (!response.ok) throw new Error("state");
      etag = response.headers.get("ETag");
      const previous = new Set(
        state ? allQueueOperations().map((item) => item.operation_id) : [],
      );
      state = await response.json();
      allQueueOperations().forEach((item) => {
        if (!previous.has(item.operation_id) && item.submittable !== false) {
          explicitSelected.add(item.operation_id);
        }
      });
      recomputeSelected();
      renderQueue();
      renderExecution();
      loadTodayCount();
      await loadHistory(false);
    }
  } catch (_error) {
    if (!sessionExpired) message("状态刷新失败，将自动重试");
  }
  if (sessionExpired) return;
  const active =
    state &&
    (state.execution.staged.length || state.execution.executing.length);
  pollTimer = setTimeout(() => loadState(false), active ? 1000 : 5000);
}

function activateTab(tabName) {
  const names = ["queue", "executing", "attention", "history"];
  names.forEach((name) => {
    const active = name === tabName;
    const tab = document.getElementById(`tab-${name}`);
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    document.getElementById(`${name}-panel`).hidden = !active;
  });
  document.querySelector(".actionbar").hidden = tabName !== "queue";
  if (tabName === "history") loadHistory(false);
}

["queue", "executing", "attention", "history"].forEach((name) => {
  document.getElementById(`tab-${name}`).addEventListener("click", () => activateTab(name));
});
document.getElementById("queue-search").addEventListener("input", renderQueue);
document.getElementById("risk-filter").addEventListener("change", renderQueue);
document.getElementById("type-filter").addEventListener("change", renderQueue);
const fieldPreviewDialog = document.getElementById("field-preview-dialog");
document.getElementById("field-preview-open").addEventListener("click", () => {
  fieldPreviewDialog.showModal();
  document.getElementById("field-preview-issue-key").focus();
});
document.getElementById("field-preview-close").addEventListener("click", () => {
  fieldPreviewDialog.close();
});
document.getElementById("field-preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  loadFieldPreview(
    document.getElementById("field-preview-issue-key").value.trim(),
  );
});

document.getElementById("select-all").addEventListener("click", () => {
  allQueueOperations().forEach((item) => {
    if (item.submittable !== false) explicitSelected.add(item.operation_id);
  });
  recomputeSelected();
  renderQueue();
});
document
  .getElementById("submit-selected")
  .addEventListener("click", () => submitSelected(false));
document
  .getElementById("cancel-selected")
  .addEventListener("click", cancelSelected);
document.getElementById("history-refresh").addEventListener("click", () => {
  historyPage = 1;
  lastHistoryRevision = -1;
  loadHistory(true);
});
document.getElementById("history-prev").addEventListener("click", () => {
  if (historyPage > 1) {
    historyPage -= 1;
    lastHistoryRevision = -1;
    loadHistory(true);
  }
});
document.getElementById("history-next").addEventListener("click", () => {
  if (historyPage < historyPages) {
    historyPage += 1;
    lastHistoryRevision = -1;
    loadHistory(true);
  }
});
document.getElementById("history-date").valueAsDate = new Date();
loadState(true);
