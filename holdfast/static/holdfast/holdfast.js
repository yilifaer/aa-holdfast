/*
 * Table tooling: labelled filters, sorting, paging and search.
 *
 * Deliberately dependency-free. Vendoring DataTables is the usual Alliance
 * Auth answer and would work fine, but for a plugin meant to be published this
 * is a hundred kilobytes of third-party bundle to keep in the repository and
 * in step with upstream, for a feature set these tables do not need. What is
 * needed is here: a "Filter by" row of labelled dropdowns, a page-size
 * selector, a search box, and click-to-sort headers.
 *
 * Markup contract:
 *   <table data-filterable>                        turns it all on
 *   <th data-filter="Label">                       adds a dropdown for that column
 *   <th data-sort="off">                           excludes a column from sorting
 *   <th data-sort-type="number|date|text">         overrides value detection
 *   <td data-filter-value="...">                   value used for filtering and
 *                                                  sorting instead of the text
 *   <tr data-filter-follows>                       detail row; shares the fate
 *                                                  of the row above it
 */
(function () {
  "use strict";

  var PAGE_SIZES = [10, 25, 50, 100, 0]; // 0 means "all"

  function cellText(cell) {
    if (!cell) return "";
    return (cell.getAttribute("data-filter-value") || cell.textContent || "")
      .trim()
      .replace(/\s+/g, " ");
  }

  function sortValue(cell, type) {
    var raw = cellText(cell);
    if (type === "number") {
      var numeric = raw.replace(/[^0-9.\-]/g, "");
      var parsed = parseFloat(numeric);
      return isNaN(parsed) ? -Infinity : parsed;
    }
    if (type === "date") {
      var stamp = Date.parse(raw);
      return isNaN(stamp) ? -Infinity : stamp;
    }
    return raw.toLowerCase();
  }

  function detectType(table, index) {
    var header = table.tHead.rows[0].cells[index];
    var declared = header.getAttribute("data-sort-type");
    if (declared) return declared;

    var numeric = 0;
    var dated = 0;
    var seen = 0;
    eachRow(table, function (row) {
      if (seen >= 12) return;
      var raw = cellText(row.cells[index]);
      if (!raw || raw === "—" || raw === "-") return;
      seen += 1;
      if (/^[\d,.\s\-+%]+$/.test(raw)) numeric += 1;
      else if (!isNaN(Date.parse(raw))) dated += 1;
    });
    if (seen && numeric / seen > 0.7) return "number";
    if (seen && dated / seen > 0.7) return "date";
    return "text";
  }

  function eachRow(table, fn) {
    Array.prototype.forEach.call(table.tBodies, function (body) {
      Array.prototype.forEach.call(body.rows, function (row) {
        if (!row.hasAttribute("data-filter-follows")) fn(row);
      });
    });
  }

  function detailRowsFor(row) {
    var found = [];
    var next = row.nextElementSibling;
    while (next && next.hasAttribute("data-filter-follows")) {
      found.push(next);
      next = next.nextElementSibling;
    }
    return found;
  }

  // ---------------------------------------------------------------- build

  function buildControls(table, state) {
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);

    var panel = document.createElement("div");
    panel.className = "holdfast-tools";

    var filters = document.createElement("div");
    filters.className = "holdfast-tools__filters";
    var legend = document.createElement("span");
    legend.className = "holdfast-tools__legend";
    legend.textContent = table.getAttribute("data-filter-legend") || "Filter by:";
    filters.appendChild(legend);

    headers.forEach(function (th, index) {
      var label = th.getAttribute("data-filter");
      if (!label) return;

      var values = new Set();
      eachRow(table, function (row) {
        var value = cellText(row.cells[index]);
        if (value && value !== "—" && value !== "-") values.add(value);
      });
      if (values.size < 2) return;

      var group = document.createElement("label");
      group.className = "holdfast-tools__group";

      var caption = document.createElement("span");
      caption.className = "holdfast-tools__label";
      caption.textContent = label;
      group.appendChild(caption);

      var select = document.createElement("select");
      select.className = "form-select form-select-sm";
      var any = document.createElement("option");
      any.value = "";
      any.textContent = "All";
      select.appendChild(any);
      Array.from(values)
        .sort(function (a, b) {
          return a.localeCompare(b, undefined, { numeric: true });
        })
        .forEach(function (value) {
          var option = document.createElement("option");
          option.value = value;
          option.textContent = value;
          select.appendChild(option);
        });
      select.dataset.columnIndex = String(index);
      state.selects.push(select);
      group.appendChild(select);
      filters.appendChild(group);
    });

    var bar = document.createElement("div");
    bar.className = "holdfast-tools__bar";

    var sizeGroup = document.createElement("label");
    sizeGroup.className = "holdfast-tools__group";
    var sizeCaption = document.createElement("span");
    sizeCaption.className = "holdfast-tools__label";
    sizeCaption.textContent = "Show";
    sizeGroup.appendChild(sizeCaption);
    var size = document.createElement("select");
    size.className = "form-select form-select-sm holdfast-tools__size";
    PAGE_SIZES.forEach(function (value) {
      var option = document.createElement("option");
      option.value = String(value);
      option.textContent = value === 0 ? "All" : String(value);
      size.appendChild(option);
    });
    size.value = String(state.pageSize);
    sizeGroup.appendChild(size);
    bar.appendChild(sizeGroup);

    var search = document.createElement("input");
    search.type = "search";
    search.className = "form-control form-control-sm holdfast-tools__search";
    search.placeholder = table.getAttribute("data-filter-placeholder") || "Search…";
    search.setAttribute("aria-label", "Search table");
    bar.appendChild(search);

    var reset = document.createElement("button");
    reset.type = "button";
    reset.className = "btn btn-sm btn-outline-secondary";
    reset.textContent = "Reset";
    bar.appendChild(reset);

    panel.appendChild(filters);
    panel.appendChild(bar);

    var footer = document.createElement("div");
    footer.className = "holdfast-tools__footer";
    var count = document.createElement("span");
    count.className = "holdfast-tools__count";
    var pager = document.createElement("div");
    pager.className = "holdfast-tools__pager";
    footer.appendChild(count);
    footer.appendChild(pager);

    state.search = search;
    state.size = size;
    state.reset = reset;
    state.count = count;
    state.pager = pager;
    return { panel: panel, footer: footer };
  }

  function makeSortable(table, state) {
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, index) {
      if (th.getAttribute("data-sort") === "off") return;
      th.classList.add("holdfast-sortable");
      th.tabIndex = 0;
      var activate = function () {
        if (state.sortIndex === index) {
          state.sortAsc = !state.sortAsc;
        } else {
          state.sortIndex = index;
          state.sortAsc = true;
          state.sortType = detectType(table, index);
        }
        state.page = 1;
        apply(table, state);
      };
      th.addEventListener("click", activate);
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
    });
  }

  // ---------------------------------------------------------------- apply

  function apply(table, state) {
    var needle = state.search.value.toLowerCase().trim();
    var active = state.selects
      .filter(function (select) { return select.value; })
      .map(function (select) {
        return { index: Number(select.dataset.columnIndex), value: select.value };
      });

    var rows = [];
    eachRow(table, function (row) { rows.push(row); });

    var matched = rows.filter(function (row) {
      var haystack = Array.prototype.map.call(row.cells, cellText).join(" ").toLowerCase();
      if (needle && haystack.indexOf(needle) === -1) return false;
      return active.every(function (filter) {
        return cellText(row.cells[filter.index]) === filter.value;
      });
    });

    if (state.sortIndex !== null) {
      var index = state.sortIndex;
      var type = state.sortType;
      var direction = state.sortAsc ? 1 : -1;
      matched.sort(function (a, b) {
        var left = sortValue(a.cells[index], type);
        var right = sortValue(b.cells[index], type);
        if (left < right) return -1 * direction;
        if (left > right) return 1 * direction;
        return 0;
      });
      // Re-insert in sorted order, dragging each row's detail rows with it.
      var body = table.tBodies[0];
      matched.forEach(function (row) {
        var details = detailRowsFor(row);
        body.appendChild(row);
        details.forEach(function (detail) { body.appendChild(detail); });
      });
      rows.forEach(function (row) {
        if (matched.indexOf(row) === -1) {
          var details = detailRowsFor(row);
          body.appendChild(row);
          details.forEach(function (detail) { body.appendChild(detail); });
        }
      });
    }

    var pageSize = state.pageSize;
    var pages = pageSize ? Math.max(Math.ceil(matched.length / pageSize), 1) : 1;
    if (state.page > pages) state.page = pages;
    var from = pageSize ? (state.page - 1) * pageSize : 0;
    var to = pageSize ? from + pageSize : matched.length;

    rows.forEach(function (row) {
      var position = matched.indexOf(row);
      var visible = position >= from && position < to;
      row.hidden = !visible;
      detailRowsFor(row).forEach(function (detail) { detail.hidden = !visible; });
    });

    state.count.textContent =
      matched.length === rows.length
        ? "Showing " + Math.min(to, matched.length) + " of " + rows.length
        : "Showing " + Math.min(to - from, matched.length - from) +
          " of " + matched.length + " filtered (" + rows.length + " total)";

    renderPager(state, pages);
    renderSortIndicators(table, state);
  }

  function renderPager(state, pages) {
    state.pager.innerHTML = "";
    if (pages <= 1) return;

    var add = function (label, page, disabled, current) {
      var button = document.createElement("button");
      button.type = "button";
      button.className =
        "btn btn-sm " + (current ? "btn-primary" : "btn-outline-secondary");
      button.textContent = label;
      button.disabled = !!disabled;
      button.addEventListener("click", function () {
        state.page = page;
        state.onChange();
      });
      state.pager.appendChild(button);
    };

    add("‹", state.page - 1, state.page === 1);
    var first = Math.max(1, state.page - 2);
    var last = Math.min(pages, first + 4);
    first = Math.max(1, last - 4);
    for (var page = first; page <= last; page += 1) {
      add(String(page), page, false, page === state.page);
    }
    add("›", state.page + 1, state.page === pages);
  }

  function renderSortIndicators(table, state) {
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, index) {
      th.classList.remove("is-sorted-asc", "is-sorted-desc");
      if (index === state.sortIndex) {
        th.classList.add(state.sortAsc ? "is-sorted-asc" : "is-sorted-desc");
      }
    });
  }

  // ---------------------------------------------------------------- init

  function init(table) {
    if (!table.tHead || !table.tHead.rows.length) return;

    // A filter bar over four rows is furniture, not help. Below the threshold
    // the table is left plain; override per table with data-min-rows.
    var minRows = Number(table.getAttribute("data-min-rows") || 8);
    var rowCount = 0;
    eachRow(table, function () { rowCount += 1; });
    if (rowCount < minRows) return;

    var state = {
      selects: [],
      page: 1,
      pageSize: Number(table.getAttribute("data-page-size") || 25),
      sortIndex: null,
      sortAsc: true,
      sortType: "text",
    };
    state.onChange = function () { apply(table, state); };

    var built = buildControls(table, state);
    table.parentNode.insertBefore(built.panel, table);
    table.parentNode.insertBefore(built.footer, table.nextSibling);
    makeSortable(table, state);

    var rerun = function () {
      state.page = 1;
      apply(table, state);
    };
    state.search.addEventListener("input", rerun);
    state.selects.forEach(function (select) {
      select.addEventListener("change", rerun);
    });
    state.size.addEventListener("change", function () {
      state.pageSize = Number(state.size.value);
      rerun();
    });
    state.reset.addEventListener("click", function () {
      state.search.value = "";
      state.selects.forEach(function (select) { select.value = ""; });
      state.sortIndex = null;
      rerun();
      state.search.focus();
    });

    apply(table, state);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table[data-filterable]").forEach(init);
  });
})();
