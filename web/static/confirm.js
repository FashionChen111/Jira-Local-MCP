"use strict";
const queueContent = document.getElementById("queue-content"),
  stagingContent = document.getElementById("staging-content"),
  executingContent = document.getElementById("executing-content"),
  historyContent = document.getElementById("history-content");
const selected = new Set(),
  explicitSelected = new Set(),
  heldSelected = new Set(),
  expanded = new Set();
let state = null,
  etag = null,
  pollTimer = null,
  historyPage = 1,
  historyPages = 1,
  lastHistoryRevision = -1;
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
function button(text, handler, className) {
  const node = el("button", className, text);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}
function message(text) {
  document.getElementById("queue-message").textContent = text || "";
}
function allQueueOperations() {
  if (!state) return [];
  return [
    ...state.queue.groups.flatMap((group) => group.operations),
    ...(state.queue.planned_groups || []).flatMap((group) => group.operations),
    ...state.queue.standalone,
  ];
}
function queueOperationMap() {
  return new Map(allQueueOperations().map((item) => [item.operation_id, item]));
}
function dependencyIds(operation) {
  return operation.unresolved_dependency_operation_ids || [];
}
function recomputeSelected() {
  const byId = queueOperationMap();
  selected.clear();
  const stack = [...explicitSelected];
  while (stack.length) {
    const id = stack.pop();
    const operation = byId.get(id);
    if (!operation || operation.submittable === false || selected.has(id)) continue;
    selected.add(id);
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
  title.append(
    el("div", "operation-title", operation.description),
    el(
      "div",
      "meta",
      `${operation.operation_type} · ${operation.operation_id} · ${operation.issue_key || "无 issue key"} · ${operation.submission_risk_level || operation.risk_level} risk`,
    ),
  );
  if (operation.issue_references && Object.keys(operation.issue_references).length) {
    const refs = Object.entries(operation.issue_references)
      .map(([field, ref]) =>
        `${field}: ${ref.operation_id}${ref.resolved_issue_key ? ` → ${ref.resolved_issue_key}` : "（待解析）"}`,
      )
      .join("；");
    title.append(el("div", "meta dependency", refs));
  }
  if (operation.submission_block_reason)
    title.append(el("div", "meta dependency-error", operation.submission_block_reason));
  head.append(title);
  if (controls.length) {
    const area = el("div", "operation-controls");
    controls.forEach((item) => area.append(item));
    head.append(area);
  }
  card.append(head);
  const details = el("details");
  details.open = expanded.has(operation.operation_id);
  details.addEventListener("toggle", () =>
    details.open
      ? expanded.add(operation.operation_id)
      : expanded.delete(operation.operation_id),
  );
  details.append(el("summary", null, "完整参数"));
  const pre = el("pre");
  pre.textContent = JSON.stringify(operation.payload, null, 2);
  details.append(pre);
  card.append(details);
  return card;
}
function renderQueue() {
  queueContent.replaceChildren();
  document.getElementById("queue-revision").textContent =
    `revision ${state.revisions.queue}`;
  const available = new Set(
    allQueueOperations().map((item) => item.operation_id),
  );
  for (const id of [...selected]) if (!available.has(id)) selected.delete(id);
  for (const id of [...explicitSelected])
    if (!available.has(id)) explicitSelected.delete(id);
  recomputeSelected();
  state.queue.groups.forEach((group, groupIndex) => {
    const box = el("div", "issue-group");
    box.append(el("h3", null, group.issue_key));
    group.operations.forEach((operation, index) => {
      const check = el("input");
      check.type = "checkbox";
      check.checked = selected.has(operation.operation_id);
      check.disabled = operation.submittable === false;
      check.title =
        selected.has(operation.operation_id) &&
        !explicitSelected.has(operation.operation_id)
          ? "因依赖关系自动勾选"
          : "";
      check.addEventListener("change", () =>
        setExplicitSelection(operation.operation_id, check.checked),
      );
      const move = (delta) => {
        const target = index + delta;
        if (target < 0 || target >= group.operations.length) return;
        [group.operations[index], group.operations[target]] = [
          group.operations[target],
          group.operations[index],
        ];
        saveLayout();
      };
      const detach = () => {
        group.operations.splice(index, 1);
        state.queue.standalone.push(operation);
        saveLayout();
      };
      box.append(
        operationCard(operation, [
          check,
          button("↑", () => move(-1)),
          button("↓", () => move(1)),
          button("转为零散", detach),
        ]),
      );
    });
    queueContent.append(box);
  });
  (state.queue.planned_groups || []).forEach((group) => {
    const box = el("div", "issue-group planned-group");
    box.append(
      el(
        "h3",
        null,
        `待创建事务 · ${group.producer_operation_id} · ${group.producer_state}`,
      ),
    );
    const producerPresent = group.operations.some(
      (item) => item.operation_id === group.producer_operation_id,
    );
    group.operations.forEach((operation, index) => {
      const check = el("input");
      check.type = "checkbox";
      check.checked = selected.has(operation.operation_id);
      check.disabled = operation.submittable === false;
      check.title =
        selected.has(operation.operation_id) &&
        !explicitSelected.has(operation.operation_id)
          ? "因依赖关系自动勾选"
          : "";
      check.addEventListener("change", () =>
        setExplicitSelection(operation.operation_id, check.checked),
      );
      const controls = [check];
      if (operation.submittable === false) {
        controls.push(
          button(
            "取消此项",
            () => cancelOperations([operation.operation_id], false),
            "danger",
          ),
        );
      }
      if (operation.operation_id !== group.producer_operation_id) {
        const move = (delta) => {
          const target = index + delta;
          const minimum = producerPresent ? 1 : 0;
          if (target < minimum || target >= group.operations.length) return;
          [group.operations[index], group.operations[target]] = [
            group.operations[target],
            group.operations[index],
          ];
          saveLayout();
        };
        controls.push(button("↑", () => move(-1)), button("↓", () => move(1)));
      }
      box.append(operationCard(operation, controls));
    });
    queueContent.append(box);
  });
  if (state.queue.standalone.length) {
    const box = el("div", "standalone");
    box.append(el("h3", null, "零散 Operations"));
    state.queue.standalone.forEach((operation, index) => {
      const check = el("input");
      check.type = "checkbox";
      check.checked = selected.has(operation.operation_id);
      check.disabled = operation.submittable === false;
      check.addEventListener("change", () =>
        setExplicitSelection(operation.operation_id, check.checked),
      );
      const controls = [check];
      if (operation.submittable === false)
        controls.push(
          button(
            "取消此项",
            () => cancelOperations([operation.operation_id], false),
            "danger",
          ),
        );
      const move = (delta) => {
        const target = index + delta;
        if (target < 0 || target >= state.queue.standalone.length) return;
        [state.queue.standalone[index], state.queue.standalone[target]] = [
          state.queue.standalone[target],
          state.queue.standalone[index],
        ];
        saveLayout();
      };
      controls.push(button("↑", () => move(-1)), button("↓", () => move(1)));
      box.append(operationCard(operation, controls));
    });
    queueContent.append(box);
  }
  if (!allQueueOperations().length)
    queueContent.append(el("div", "empty", "当前队列为空"));
}
async function saveLayout() {
  const body = {
    expected_queue_revision: state.revisions.queue,
    groups: state.queue.groups.map((group) => ({
      issue_key: group.issue_key,
      operation_ids: group.operations.map((item) => item.operation_id),
    })),
    planned_groups: (state.queue.planned_groups || []).map((group) => ({
      producer_operation_id: group.producer_operation_id,
      operation_ids: group.operations.map((item) => item.operation_id),
    })),
    standalone_operation_ids: state.queue.standalone.map(
      (item) => item.operation_id,
    ),
  };
  const response = await fetch("/api/queue/reorder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    message("后端状态已更新，正在刷新；请重新确认排序。");
    etag = null;
    await loadState(true);
    return;
  }
  message("排序已持久化");
  etag = null;
  await loadState(true);
}
function renderExecution() {
  stagingContent.replaceChildren();
  const heldIds = new Set(
    state.execution.held.map((item) => item.operation_id),
  );
  for (const id of [...heldSelected])
    if (!heldIds.has(id)) heldSelected.delete(id);
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
    stagingContent.append(bulk);
  }
  const staged = [
    ...state.execution.staged,
    ...state.execution.held,
    ...state.execution.indeterminate,
  ];
  staged.forEach((operation) => {
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
      check.addEventListener("change", () =>
        check.checked
          ? heldSelected.add(operation.operation_id)
          : heldSelected.delete(operation.operation_id),
      );
      controls.push(
        check,
        button("恢复", () => resolveHeld([operation.operation_id], "resume")),
        button("退回队列", () =>
          resolveHeld([operation.operation_id], "requeue"),
        ),
        button(
          "放弃",
          () => resolveHeld([operation.operation_id], "abandon"),
          "danger",
        ),
      );
    }
    const card = operationCard(operation, controls);
    card.prepend(el("span", `status ${operation.state}`, operation.state));
    stagingContent.append(card);
  });
  if (!staged.length)
    stagingContent.append(el("div", "empty", "暂无暂存或阻塞 operation"));
  executingContent.replaceChildren();
  state.execution.executing.forEach((operation) => {
    const card = operationCard(operation);
    card.prepend(el("span", "status", "executing"));
    executingContent.append(card);
  });
  if (!state.execution.executing.length)
    executingContent.append(el("div", "empty", "当前没有 Jira 写操作执行中"));
}
async function resolveIndeterminate(operation, resolution) {
  let high = false;
  if (
    resolution === "not_applied" &&
    operation.submission_risk_level === "high"
  )
    high = confirm("这是高风险 operation，确认核实为未生效并重新入队？");
  if (
    resolution === "not_applied" &&
    operation.submission_risk_level === "high" &&
    !high
  )
    return;
  let newIssueKey = null;
  if (
    resolution === "succeeded" &&
    ["create_issue", "clone_issue"].includes(operation.operation_type)
  ) {
    newIssueKey = prompt("请输入 Jira 实际生成的事务 Key（例如 PROJ-123）");
    if (newIssueKey === null) return;
    newIssueKey = newIssueKey.trim();
    if (!/^[A-Z][A-Z0-9]+-\d+$/.test(newIssueKey)) {
      message("事务 Key 格式无效");
      return;
    }
  }
  await mutate("/api/execution/indeterminate/resolve", {
    operation_id: operation.operation_id,
    resolution,
    high_risk_confirmed: high,
    new_issue_key: newIssueKey,
  });
}
async function resolveHeld(ids, action) {
  if (!ids.length) {
    message("请先勾选 held operation");
    return;
  }
  if (action === "abandon" && !confirm("确认放弃选中的 held operation？"))
    return;
  await mutate("/api/execution/held/resolve", { operation_ids: ids, action });
}
async function mutate(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    message(data.error || "操作失败");
    return;
  }
  etag = null;
  await loadState(true);
}
async function submitSelected(highRiskConfirmed = false) {
  const ids = [...selected];
  if (!ids.length) {
    message("请先勾选 operation");
    return;
  }
  const response = await fetch("/api/queue/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_queue_revision: state.revisions.queue,
      operation_ids: ids,
      high_risk_confirmed: highRiskConfirmed,
    }),
  });
  const data = await response.json();
  if (
    response.status === 409 &&
    data.code === "confirmation_required" &&
    !highRiskConfirmed
  ) {
    if (confirm("本次选择包含高风险操作，确认提交执行？"))
      return submitSelected(true);
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
async function cancelSelected() {
  const ids = [...explicitSelected];
  if (!ids.length) {
    message("请先勾选 operation");
    return;
  }
  if (!confirm("确认从队列取消选中的 operation？这不会写入 Jira。")) return;
  await cancelOperations(ids, false);
}
async function cancelOperations(ids, cascadeConfirmed) {
  const response = await fetch("/api/queue/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_queue_revision: state.revisions.queue,
      operation_ids: ids,
      cascade_confirmed: cascadeConfirmed,
    }),
  });
  const data = await response.json();
  if (
    response.status === 409 &&
    data.code === "dependency_confirmation_required" &&
    !cascadeConfirmed
  ) {
    const requested = new Set(ids);
    const dependentCount = (data.affected_operation_ids || []).filter(
      (id) => !requested.has(id),
    ).length;
    const totalCount = (data.affected_operation_ids || []).length;
    if (
      confirm(
        `所选创建/克隆项仍有 ${dependentCount} 个队列依赖，`
          + `将原子取消共 ${totalCount} 个 operation。确认继续？`,
      )
    )
      return cancelOperations(ids, true);
    return;
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
}
async function loadHistory(force = false) {
  if (!state) return;
  if (!force && lastHistoryRevision === state.revisions.history) return;
  const date = document.getElementById("history-date").value;
  let query = `page=${historyPage}&page_size=50`;
  if (date) {
    const start = new Date(`${date}T00:00:00`),
      end = new Date(start);
    end.setDate(end.getDate() + 1);
    query += `&start_at=${start.getTime()}&end_at=${end.getTime()}`;
  }
  const response = await fetch(`/api/execution-history?${query}`);
  if (!response.ok) return;
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
        item.resolution ? `${item.outcome} · ${item.resolution}` : item.outcome,
      ),
    );
    card.append(el("div", "meta", new Date(item.started_at).toLocaleString()));
    if (item.summary) card.append(el("div", "meta", item.summary));
    if (item.error) card.append(el("div", "meta", item.error));
    historyContent.append(card);
  });
  if (!data.items.length)
    historyContent.append(el("div", "empty", "该日期没有执行记录"));
  document.getElementById("history-page").textContent =
    `${historyPage} / ${historyPages}`;
  document.getElementById("history-prev").disabled = historyPage <= 1;
  document.getElementById("history-next").disabled =
    historyPage >= historyPages;
}
async function loadState(force = false) {
  clearTimeout(pollTimer);
  const headers = {};
  if (etag && !force) headers["If-None-Match"] = etag;
  try {
    const response = await fetch("/api/state", { headers });
    if (response.status !== 304) {
      if (!response.ok) throw new Error("state");
      etag = response.headers.get("ETag");
      const previous = new Set(
        state ? allQueueOperations().map((item) => item.operation_id) : [],
      );
      state = await response.json();
      allQueueOperations().forEach((item) => {
        if (!previous.has(item.operation_id)) {
          item.__new = true;
          if (item.submittable !== false)
            explicitSelected.add(item.operation_id);
        }
      });
      recomputeSelected();
      renderQueue();
      renderExecution();
      await loadHistory(false);
    }
  } catch (_error) {
    message("状态刷新失败，将自动重试");
  }
  const active =
    state &&
    (state.execution.staged.length || state.execution.executing.length);
  pollTimer = setTimeout(() => loadState(false), active ? 1000 : 5000);
}
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
    historyPage--;
    lastHistoryRevision = -1;
    loadHistory(true);
  }
});
document.getElementById("history-next").addEventListener("click", () => {
  if (historyPage < historyPages) {
    historyPage++;
    lastHistoryRevision = -1;
    loadHistory(true);
  }
});
document.getElementById("history-date").valueAsDate = new Date();
loadState(true);
