(() => {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const $ = (selector) => document.querySelector(selector);
  const notice = $("#notice");
  let dataset = null;
  let chartConfig = null;
  let noticeTimer;

  function showNotice(message, isError = false) {
    notice.textContent = message;
    notice.classList.toggle("error", isError);
    notice.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice.hidden = true; }, 5500);
  }

  async function request(url, options = {}) {
    const headers = { "X-CSRF-Token": csrf, ...(options.headers || {}) };
    if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
    try {
      const response = await fetch(url, { ...options, headers });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok) {
        const body = contentType.includes("application/json") ? await response.json() : {};
        throw new Error(body.error || `Request failed (${response.status}).`);
      }
      return contentType.includes("application/json") ? response.json() : response;
    } catch (error) {
      if (error instanceof TypeError && error.message === "Failed to fetch") {
        throw new Error("Could not reach the app server. Check that the Flask app is running and the route is available.");
      }
      throw error;
    }
  }

  const number = (value) => Number(value || 0).toLocaleString();
  const field = (label, element) => {
    const wrapper = document.createElement("label");
    wrapper.className = "filter-control";
    const caption = document.createElement("span");
    caption.textContent = label;
    wrapper.append(caption, element);
    return wrapper;
  };

  function setSelect(selector, columns, firstLabel = null, numericOnly = false) {
    const select = $(selector);
    const previous = select.value;
    select.replaceChildren();
    if (firstLabel !== null) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = firstLabel;
      select.append(option);
    }
    const choices = numericOnly
      ? columns.filter((column) => ["integer", "float"].includes(column.guessed_type))
      : columns;
    choices.forEach((column) => {
      const option = document.createElement("option");
      option.value = column.name;
      option.textContent = column.name;
      select.append(option);
    });
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  }

  function renderFilters(profile) {
    const container = $("#filter-controls");
    container.replaceChildren();
    const categories = profile.filter_options || {};
    for (const column of profile.summary.columns) {
      const columnName = column.name;
      const stats = profile.summary.numeric_stats[columnName];
      if (stats) {
        const low = document.createElement("input");
        low.type = "number";
        low.step = "any";
        low.placeholder = `Min ${stats.min}`;
        low.dataset.filter = columnName;
        low.dataset.bound = "min";
        const high = document.createElement("input");
        high.type = "number";
        high.step = "any";
        high.placeholder = `Max ${stats.max}`;
        high.dataset.filter = columnName;
        high.dataset.bound = "max";
        const wrap = document.createElement("div");
        wrap.className = "filter-control";
        const label = document.createElement("span");
        label.textContent = columnName;
        wrap.append(label, low, high);
        container.append(wrap);
      } else if (column.guessed_type === "date") {
        const low = document.createElement("input");
        low.type = "date";
        low.dataset.filter = columnName;
        low.dataset.bound = "min";
        const high = document.createElement("input");
        high.type = "date";
        high.dataset.filter = columnName;
        high.dataset.bound = "max";
        const wrap = document.createElement("div");
        wrap.className = "filter-control";
        const label = document.createElement("span");
        label.textContent = columnName;
        wrap.append(label, low, high);
        container.append(wrap);
      } else if (categories[columnName]?.length) {
        const select = document.createElement("select");
        select.dataset.filter = columnName;
        const all = document.createElement("option");
        all.value = "";
        all.textContent = "All";
        select.append(all);
        categories[columnName].forEach((value) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value.length > 27 ? `${value.slice(0, 27)}…` : value;
          select.append(option);
        });
        container.append(field(columnName, select));
      } else if (
        column.guessed_type === "string" &&
        /region|department|product|gender|category|customer|salary|segment/i.test(columnName)
      ) {
        const input = document.createElement("input");
        input.type = "search";
        input.placeholder = "Contains…";
        input.dataset.filter = columnName;
        input.dataset.match = "contains";
        container.append(field(columnName, input));
      }
    }
  }

  function filterPayload() {
    const filters = {};
    document.querySelectorAll("[data-filter]").forEach((input) => {
      const column = input.dataset.filter;
      if (input.dataset.match === "contains") {
        filters[column] = { contains: input.value };
      } else if (input.dataset.bound) {
        const group = filters[column] || {};
        if (input.value !== "") group[input.dataset.bound] = input.value;
        filters[column] = group;
      } else if (input.value) {
        filters[column] = input.value;
      }
    });
    return { search: $("#search-input").value, column: $("#search-column").value, filters };
  }

  function renderTable(rows) {
    const table = $("#data-table");
    const head = table.querySelector("thead");
    const body = table.querySelector("tbody");
    head.replaceChildren();
    body.replaceChildren();
    const header = document.createElement("tr");
    dataset.column_names.forEach((name) => {
      const cell = document.createElement("th");
      cell.textContent = name;
      header.append(cell);
    });
    head.append(header);
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      dataset.column_names.forEach((name) => {
        const cell = document.createElement("td");
        const value = row[name];
        cell.textContent = value === null || value === undefined ? "—" : String(value);
        cell.title = cell.textContent;
        tr.append(cell);
      });
      body.append(tr);
    });
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = Math.max(dataset.column_names.length, 1);
      td.textContent = "No rows match these filters.";
      tr.append(td);
      body.append(tr);
    }
  }

  async function refreshData(updateChart = true) {
    if (!dataset) return;
    const result = await request("/api/data", { method: "POST", body: JSON.stringify(filterPayload()) });
    renderTable(result.rows);
    $("#filtered-count").textContent = `${number(result.total)} matching rows`;
    if (updateChart && chartConfig) buildChart(false);
  }

  function renderQuality(summary) {
    const missingColumns = summary.columns.filter((column) => column.missing > 0).length;
    const emailIssues = summary.columns.reduce((sum, column) => sum + column.invalid_email, 0);
    const phoneIssues = summary.columns.reduce((sum, column) => sum + column.invalid_phone, 0);
    const mixed = summary.columns.filter((column) => column.mixed_types).length;
    const typeMismatches = summary.columns.filter((column) => column.type_mismatch).length;
    const issueCards = [
      ["Missing values", `${number(summary.missing)} across ${missingColumns} columns`, summary.missing],
      ["Duplicate rows", `${number(summary.duplicate_rows)} exact matches`, summary.duplicate_rows],
      ["Outliers (IQR)", `${number(Object.values(summary.outliers).reduce((sum, item) => sum + item.IQR, 0))} across numeric columns`, Object.values(summary.outliers).some((item) => item.IQR > 0)],
      ["Invalid emails", `${number(emailIssues)} in email columns`, emailIssues],
      ["Invalid phones", `${number(phoneIssues)} in phone columns`, phoneIssues],
      ["Mixed data types", `${number(mixed)} columns may need conversion`, mixed],
      ["Type mismatch", `${number(typeMismatches)} columns differ from detected type`, typeMismatches],
      ["Blank rows / columns", `${number(summary.blank_rows)} rows · ${number(summary.blank_columns)} columns`, summary.blank_rows + summary.blank_columns],
      ["Negative values", `${number(summary.columns.reduce((sum, column) => sum + column.negative, 0))} numeric entries`, summary.columns.some((column) => column.negative)],
      ["Special characters", `${number(summary.columns.reduce((sum, column) => sum + column.special_characters, 0))} values flagged`, summary.columns.some((column) => column.special_characters)],
    ];
    const container = $("#quality-summary");
    container.replaceChildren();
    issueCards.forEach(([title, detail, count]) => {
      const card = document.createElement("div");
      card.className = `quality-item${count ? " warn" : ""}`;
      const b = document.createElement("b");
      b.textContent = title;
      const span = document.createElement("span");
      span.textContent = detail;
      card.append(b, span);
      container.append(card);
    });
  }

  function renderKpis(summary) {
    $("#kpi-rows").textContent = number(summary.rows);
    $("#kpi-columns").textContent = number(summary.columns_count);
    $("#kpi-missing").textContent = number(summary.missing);
    $("#kpi-duplicates").textContent = number(summary.duplicate_rows);
    $("#missing-note").textContent = summary.rows && summary.columns_count
      ? `${((summary.missing / (summary.rows * summary.columns_count)) * 100).toFixed(1)}% of cells`
      : "Across all columns";
    const strip = $("#chart-kpis");
    strip.replaceChildren();
    const metrics = [
      ["Rows", summary.rows],
      ["Columns", summary.columns_count],
      ["Missing", summary.missing],
      ["Duplicates", summary.duplicate_rows],
    ];
    metrics.forEach(([label, value]) => {
      const item = document.createElement("div");
      item.className = "mini-kpi";
      const strong = document.createElement("b");
      strong.textContent = number(value);
      item.append(document.createTextNode(`${label}:`), strong);
      strip.append(item);
    });
  }

  function renderAnalysis(summary) {
    const statsTable = $("#stats-table");
    const statHead = statsTable.querySelector("thead");
    const statBody = statsTable.querySelector("tbody");
    statHead.replaceChildren();
    statBody.replaceChildren();
    const statColumns = ["Column", "Mean", "Median", "Mode", "Std. dev.", "Variance", "Min", "Q1", "Q2", "Q3", "Max"];
    const statHeader = document.createElement("tr");
    statColumns.forEach((name) => {
      const th = document.createElement("th");
      th.textContent = name;
      statHeader.append(th);
    });
    statHead.append(statHeader);
    const format = (value) => value === null || value === undefined ? "—" : Number.isFinite(Number(value)) ? Number(value).toLocaleString(undefined, { maximumSignificantDigits: 5 }) : String(value);
    Object.entries(summary.numeric_stats).forEach(([name, stats]) => {
      const tr = document.createElement("tr");
      [name, stats.mean, stats.median, stats.mode, stats.std, stats.variance, stats.min, stats.q1, stats.q2, stats.q3, stats.max].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = format(value);
        tr.append(td);
      });
      statBody.append(tr);
    });
    if (!statBody.children.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = statColumns.length;
      td.textContent = "No numeric columns are available for statistical analysis.";
      tr.append(td);
      statBody.append(tr);
    }

    const missingTable = $("#missing-table");
    const missingHead = missingTable.querySelector("thead");
    const missingBody = missingTable.querySelector("tbody");
    missingHead.replaceChildren();
    missingBody.replaceChildren();
    const headers = ["Column", "Detected type", "Current type", "Missing", "Unique values", "Type mismatch"];
    const header = document.createElement("tr");
    headers.forEach((name) => {
      const th = document.createElement("th");
      th.textContent = name;
      header.append(th);
    });
    missingHead.append(header);
    summary.columns.forEach((column) => {
      const tr = document.createElement("tr");
      [column.name, column.guessed_type, column.dtype, number(column.missing), number(column.unique), column.type_mismatch ? "Yes" : "No"].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = String(value);
        tr.append(td);
      });
      missingBody.append(tr);
    });
  }

  function renderProfile(profile, refresh = true) {
    dataset = profile;
    $("#upload-panel").hidden = false;
    $("#workspace").hidden = false;
    $("#dataset-name").textContent = profile.filename;
    $("#kpi-rows").textContent = number(profile.summary.rows);
    $("#kpi-columns").textContent = number(profile.summary.columns_count);
    $("#kpi-missing").textContent = number(profile.summary.missing);
    $("#kpi-duplicates").textContent = number(profile.summary.duplicate_rows);
    renderKpis(profile.summary);
    renderAnalysis(profile.summary);
    renderQuality(profile.summary);
    renderFilters(profile);
    setSelect("#search-column", profile.summary.columns, "All columns");
    setSelect("#null-column", profile.summary.columns, "All columns");
    setSelect("#type-column", profile.summary.columns);
    setSelect("#chart-x", profile.summary.columns);
    setSelect("#chart-y", profile.summary.columns, "Count rows");
    setSelect("#chart-size", profile.summary.columns, "No size", true);
    setSelect("#outlier-column", profile.summary.columns, "Choose numeric column", true);
    if (!$("#chart-y").value) $("#chart-y").value = "";
    const numeric = profile.summary.columns.find((column) => ["integer", "float"].includes(column.guessed_type));
    if (numeric && !chartConfig) $("#chart-y").value = numeric.name;
    const category = profile.summary.columns.find((column) => !["integer", "float"].includes(column.guessed_type));
    if (category) $("#chart-x").value = category.name;
    if (refresh) refreshData().catch((error) => showNotice(error.message, true));
  }

  async function uploadFile(file) {
    const form = new FormData();
    form.append("file", file);
    showNotice(`Loading ${file.name}…`);
    try {
      const result = await request("/api/upload", { method: "POST", body: form });
      renderProfile(result.dataset, false);
      await refreshData(false);
      showNotice(`${file.name} loaded. Review the quality report and choose any cleaning actions.`);
      $("#chart").hidden = false;
      $("#chart-empty").hidden = true;
      await buildChart();
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      $("#file-input").value = "";
    }
  }

  async function clean(action) {
    const payload = { action };
    if (action === "fill_missing") {
      payload.strategy = $("#null-strategy").value;
      payload.column = $("#null-column").value;
      payload.value = $("#custom-value").value;
    }
    if (action === "drop_missing_rows") payload.column = $("#null-column").value;
    if (action === "drop_missing_columns") payload.only_incomplete = false;
    if (action === "convert_type") {
      payload.column = $("#type-column").value;
      payload.target = $("#target-type").value;
    }
    if (action === "drop_column") payload.column = $("#type-column").value;
    if (action === "remove_outliers") {
      payload.column = $("#outlier-column").value;
      payload.method = $("#outlier-method").value;
    }
    try {
      const result = await request("/api/clean", { method: "POST", body: JSON.stringify(payload) });
      renderProfile(result.dataset);
      const conversionNote = action === "auto_convert_types"
        ? ` Converted ${result.converted.length} columns${result.skipped.length ? `; skipped ${result.skipped.join(", ")} because conversion could lose values` : ""}.`
        : "";
      showNotice(`Cleaning action applied. Your analysis now reflects the updated dataset.${conversionNote}`);
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  async function buildChart(showMessage = true) {
    if (!dataset) return;
    chartConfig = {
      ...filterPayload(),
      type: $("#chart-type").value,
      x: $("#chart-x").value,
      y: $("#chart-y").value,
      size: $("#chart-size").value,
      color: $("#chart-color").value,
    };
    try {
      const result = await request("/api/chart", { method: "POST", body: JSON.stringify(chartConfig) });
      $("#chart-empty").hidden = true;
      $("#chart").hidden = false;
      await Plotly.react("chart", result.figure.data, result.figure.layout, { responsive: true, displaylogo: false });
    } catch (error) {
      if (showMessage) showNotice(error.message, true);
    }
  }

  async function runAi(mode) {
    const resultBox = $("#ai-result");
    resultBox.hidden = false;
    if (!dataset) {
      resultBox.textContent = "Upload a dataset before generating AI storytelling or recommendations.";
      showNotice("Upload a dataset before generating AI storytelling or recommendations.", true);
      return;
    }
    resultBox.textContent = "Analyzing your dataset…";
    try {
      const result = await request("/api/ai", { method: "POST", body: JSON.stringify({ mode }) });
      resultBox.textContent = result.text;
    } catch (error) {
      const message = error.message.includes("Failed to fetch") || error.message.includes("Could not reach the app server")
        ? "Could not reach the app server. Make sure Flask is running, then refresh the page and try again."
        : error.message;
      resultBox.textContent = message;
      showNotice(message, true);
    }
  }

  async function exportFile(kind) {
    if (kind === "png") {
      if (!dataset || !chartConfig || !$("#chart").data) {
        showNotice("Build a chart before downloading a dashboard image.", true);
        return;
      }
      try {
        await Plotly.downloadImage("chart", {
          format: "png",
          width: 1400,
          height: 800,
          filename: "clearview-dashboard",
        });
        showNotice("Your dashboard image is ready.");
      } catch (error) {
        showNotice(`Dashboard image export failed: ${error.message}`, true);
      }
      return;
    }
    try {
      const response = await request(`/api/export/${kind}`, { method: "POST", body: JSON.stringify(chartConfig || {}) });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `clearview-report.${kind === "excel" ? "xlsx" : kind === "pptx" ? "pptx" : kind}`;
      link.click();
      URL.revokeObjectURL(url);
      showNotice("Your download is ready.");
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  $("#file-input").addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    if (file) uploadFile(file);
  });
  const dropZone = $("#drop-zone");
  ["dragenter", "dragover"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropZone.addEventListener(name, (event) => {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  }));
  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  });

  document.querySelectorAll("[data-clean]").forEach((button) => {
    button.addEventListener("click", () => clean(button.dataset.clean));
  });
  document.querySelectorAll("[data-ai]").forEach((button) => {
    button.addEventListener("click", () => runAi(button.dataset.ai));
  });
  document.querySelectorAll("[data-export]").forEach((button) => {
    button.addEventListener("click", () => exportFile(button.dataset.export));
  });
  $(".export-chart").addEventListener("click", () => exportFile("png"));
  $("#build-chart").addEventListener("click", () => buildChart());
  $("#chart-type").addEventListener("change", () => buildChart());
  $("#chart-color").addEventListener("change", () => buildChart());
  $("#null-strategy").addEventListener("change", (event) => {
    $("#custom-value").hidden = event.target.value !== "custom";
  });
  let filterTimer;
  $("#search-input").addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => refreshData().catch((error) => showNotice(error.message, true)), 250);
  });
  $("#search-column").addEventListener("change", () => refreshData().catch((error) => showNotice(error.message, true)));
  $("#filter-controls").addEventListener("change", () => refreshData().catch((error) => showNotice(error.message, true)));
  $("#filter-controls").addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => refreshData().catch((error) => showNotice(error.message, true)), 300);
  });
  $("#clear-filters").addEventListener("click", () => {
    $("#search-input").value = "";
    $("#search-column").value = "*";
    $("#filter-controls").querySelectorAll("input").forEach((input) => { input.value = ""; });
    $("#filter-controls").querySelectorAll("select").forEach((select) => { select.value = ""; });
    refreshData().catch((error) => showNotice(error.message, true));
  });

  const savedTheme = localStorage.getItem("clearview-theme");
  if (savedTheme === "dark") document.body.classList.add("dark");
  $("#theme-toggle").addEventListener("click", () => {
    document.body.classList.toggle("dark");
    localStorage.setItem("clearview-theme", document.body.classList.contains("dark") ? "dark" : "light");
  });

  request("/api/state", { method: "GET" }).then((result) => {
    if (result.dataset) renderProfile(result.dataset);
  }).catch((error) => showNotice(error.message, true));
})();
