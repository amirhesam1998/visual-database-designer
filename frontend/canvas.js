// Visual Database Designer — drag & drop canvas (Phase 6F + enhancements Phase 1, 2 & 3).
//
// A no-build-step React Flow app. It can:
//   * generate a schema from a text prompt (POST /design)
//   * edit/rename/delete/duplicate tables; assign a colour-coded group (#6)
//   * edit/add/delete columns (type, length, constraints, default); apply field presets (#12)
//   * reference reusable named enums per column (#13)
//   * edit table settings: description (#16), composite-key view (#14), explicit indexes (#15)
//   * set a relationship's type + on delete/update via an edge editor
//   * search/filter tables (#7), undo/redo (#10), zoom controls (#11)
//   * view a Mermaid ERD (ERD tab); save/compare schema versions → migration (#9, Versions tab)
//   * validate (POST /validate); export (incl. markdown docs) + generate models/CRUD (Code tab)
import React, { useState, useCallback, useMemo, useEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import ReactFlow, {
  addEdge, Background, Controls, MiniMap, Panel, useReactFlow,
  applyNodeChanges, applyEdgeChanges,
} from 'reactflow';
import TableNode from '/static/components/TableNode.js';

const e = React.createElement;

const FIELD_TYPES = ['bigint', 'integer', 'varchar', 'text', 'boolean', 'decimal', 'date', 'datetime', 'timestamp', 'json', 'uuid', 'enum', 'foreign_id'];
const RELATION_TYPES = ['one_to_one', 'one_to_many', 'many_to_one', 'many_to_many', 'polymorphic'];
const REFERENTIAL = ['cascade', 'restrict', 'set_null', 'no_action'];
const INDEX_TYPES = ['btree', 'fulltext'];
const CARDINALITY = { one_to_one: '1:1', one_to_many: '1:∞', many_to_one: '∞:1', many_to_many: '∞:∞', polymorphic: 'poly' };
const GROUP_COLORS = ['#4f46e5', '#0891b2', '#16a34a', '#d97706', '#db2777', '#7c3aed', '#dc2626', '#0d9488'];

function groupColor(name) {
  if (!name) return null;
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return GROUP_COLORS[h % GROUP_COLORS.length];
}

// ---------------------------------------------------------------------------
// schema <-> graph
// ---------------------------------------------------------------------------

function schemaToGraph(schema) {
  const tables = schema.tables || [];
  const nodes = tables.map((t, i) => makeNode(t, { x: 60 + (i % 4) * 300, y: 80 + Math.floor(i / 4) * 280 }));
  const edges = (schema.relations || []).map((r, i) => makeEdge(r, i));
  return { nodes, edges };
}

function makeNode(table, position) {
  return {
    id: table.name,
    type: 'table',
    position,
    data: {
      id: table.name,
      label: table.name,
      fields: table.fields || [],
      indexes: table.indexes || [],
      soft_delete: !!table.soft_delete,
      timestamps: table.timestamps !== false,
      group: table.group || null,
      description: table.description || null,
    },
  };
}

function makeEdge(relation, i) {
  return {
    id: `edge-${relation.from_table}-${relation.to_table}-${i}`,
    source: relation.from_table,
    target: relation.to_table,
    label: CARDINALITY[relation.type] || relation.type,
    animated: true,
    data: { relation },
  };
}

function graphToSchema(nodes, edges, base, enums) {
  const tables = nodes.map((n) => ({
    name: n.data.label,
    fields: n.data.fields || [],
    indexes: n.data.indexes || [],
    soft_delete: !!n.data.soft_delete,
    timestamps: n.data.timestamps !== false,
    group: n.data.group || null,
    description: n.data.description || null,
  }));
  const byName = Object.fromEntries(tables.map((t) => [t.name, t]));
  edges.forEach((edge) => {
    const owner = byName[edge.source];
    if (!owner) return;
    owner.relations = owner.relations || [];
    const rel = (edge.data && edge.data.relation) || {};
    owner.relations.push({
      from_table: edge.source,
      from_field: rel.from_field || `${edge.target.replace(/s$/, '')}_id`,
      to_table: edge.target,
      to_field: rel.to_field || 'id',
      type: rel.type || 'many_to_one',
      on_delete: rel.on_delete || 'cascade',
      on_update: rel.on_update || 'cascade',
    });
  });
  return { ...(base || {}), type: 'sql', driver: (base && base.driver) || 'postgresql', tables, enums: enums || [] };
}

async function api(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return res.json();
}

// ---------------------------------------------------------------------------
// small form helpers
// ---------------------------------------------------------------------------

function row(label, control) {
  return e('label', { className: 'field-row', key: label }, e('span', null, label), control);
}
function checkbox(label, value, onChange) {
  return e('label', { className: 'field-row checkbox', key: label }, e('span', null, label),
    e('input', { type: 'checkbox', checked: !!value, onChange: (ev) => onChange(ev.target.checked) }));
}
function selectEl(value, onChange, options) {
  return e('select', { value, onChange: (ev) => onChange(ev.target.value) },
    options.map((o) => e('option', { key: o.key, value: o.key }, o.label)));
}

// ---------------------------------------------------------------------------
// Column editor (enum_ref #13 + presets #12)
// ---------------------------------------------------------------------------

function ColumnEditor({ field, presets, enums, onChange, onClose, onDelete, onAddEnum }) {
  const set = (patch) => onChange({ ...field, ...patch });
  const applyPreset = (label) => { const p = presets.find((x) => x.label === label); if (p) onChange({ ...p.field }); };
  return e(
    'div',
    { className: 'side-panel' },
    e('div', { className: 'side-head' }, e('strong', null, `Column: ${field.name}`), e('button', { className: 'icon-btn', onClick: onClose }, '×')),
    presets && presets.length
      ? row('Apply preset', e('select', { value: '', onChange: (ev) => applyPreset(ev.target.value) },
          e('option', { value: '' }, '— choose a common column —'), presets.map((p) => e('option', { key: p.label, value: p.label }, p.label))))
      : null,
    row('Name', e('input', { value: field.name, onChange: (ev) => set({ name: ev.target.value }) })),
    row('Type', e('select', { value: field.type, onChange: (ev) => set({ type: ev.target.value }) }, FIELD_TYPES.map((t) => e('option', { key: t, value: t }, t)))),
    field.type === 'varchar' ? row('Length', e('input', { type: 'number', value: field.length || '', onChange: (ev) => set({ length: ev.target.value ? Number(ev.target.value) : null }) })) : null,
    field.type === 'enum'
      ? row('Reusable enum', e('select', { value: field.enum_ref || '', onChange: (ev) => { if (ev.target.value === '__new__') { onAddEnum(); } else { set({ enum_ref: ev.target.value || null, values: null }); } } },
          e('option', { value: '' }, '— inline values —'),
          enums.map((en) => e('option', { key: en.name, value: en.name }, `${en.name} (${en.values.join(', ')})`)),
          e('option', { value: '__new__' }, '＋ new enum…')))
      : null,
    field.type === 'enum' && !field.enum_ref ? row('Inline values (comma)', e('input', { value: (field.values || []).join(','), onChange: (ev) => set({ values: ev.target.value.split(',').map((s) => s.trim()).filter(Boolean) }) })) : null,
    checkbox('Primary key', field.primary_key, (v) => set({ primary_key: v })),
    checkbox('Auto increment', field.auto_increment, (v) => set({ auto_increment: v })),
    checkbox('Nullable', field.nullable, (v) => set({ nullable: v })),
    checkbox('Unique', field.unique, (v) => set({ unique: v })),
    checkbox('Indexed', field.indexed, (v) => set({ indexed: v })),
    row('Default', e('input', { value: field.default == null ? '' : field.default, onChange: (ev) => set({ default: ev.target.value || null }) })),
    row('Description (comment)', e('input', { value: field.description || '', onChange: (ev) => set({ description: ev.target.value || null }) })),
    e('div', { className: 'side-foot' }, e('button', { className: 'danger', onClick: onDelete }, 'Delete column'))
  );
}

function RelationEditor({ relation, onChange, onClose, onDelete }) {
  const set = (patch) => onChange({ ...relation, ...patch });
  return e(
    'div',
    { className: 'side-panel' },
    e('div', { className: 'side-head' }, e('strong', null, `Relationship: ${relation.from_table} → ${relation.to_table}`), e('button', { className: 'icon-btn', onClick: onClose }, '×')),
    row('Type', e('select', { value: relation.type, onChange: (ev) => set({ type: ev.target.value }) }, RELATION_TYPES.map((t) => e('option', { key: t, value: t }, `${t} (${CARDINALITY[t]})`)))),
    row('FK field', e('input', { value: relation.from_field || '', onChange: (ev) => set({ from_field: ev.target.value }) })),
    row('On delete', e('select', { value: relation.on_delete || 'cascade', onChange: (ev) => set({ on_delete: ev.target.value }) }, REFERENTIAL.map((t) => e('option', { key: t, value: t }, t)))),
    row('On update', e('select', { value: relation.on_update || 'cascade', onChange: (ev) => set({ on_update: ev.target.value }) }, REFERENTIAL.map((t) => e('option', { key: t, value: t }, t)))),
    e('div', { className: 'side-foot' }, e('button', { className: 'danger', onClick: onDelete }, 'Delete relationship'))
  );
}

// Table settings: description (#16), composite-key view (#14), indexes (#15), group (#6)
function TableEditor({ table, onChange, onClose }) {
  const data = table.data;
  const set = (patch) => onChange({ ...data, ...patch });
  const pks = (data.fields || []).filter((f) => f.primary_key).map((f) => f.name);
  const setIndex = (i, patch) => set({ indexes: data.indexes.map((ix, j) => (j === i ? { ...ix, ...patch } : ix)) });
  const addIndex = () => set({ indexes: [...(data.indexes || []), { name: '', columns: [], unique: false, type: 'btree' }] });
  const removeIndex = (i) => set({ indexes: data.indexes.filter((_, j) => j !== i) });
  return e(
    'div',
    { className: 'side-panel' },
    e('div', { className: 'side-head' }, e('strong', null, `Table: ${data.label}`), e('button', { className: 'icon-btn', onClick: onClose }, '×')),
    row('Description (comment)', e('textarea', { rows: 2, value: data.description || '', onChange: (ev) => set({ description: ev.target.value || null }) })),
    row('Group / domain', e('input', { value: data.group || '', onChange: (ev) => set({ group: ev.target.value || null }) })),
    checkbox('Timestamps (created/updated)', data.timestamps, (v) => set({ timestamps: v })),
    checkbox('Soft delete (deleted_at)', data.soft_delete, (v) => set({ soft_delete: v })),
    e('div', { className: 'side-section' }, e('span', { className: 'section-label' }, 'Primary key'),
      e('div', { className: 'pk-view' }, pks.length ? pks.join(' + ') + (pks.length > 1 ? '  (composite)' : '') : '— none — tick "Primary key" on columns')),
    e('div', { className: 'side-section' },
      e('div', { className: 'section-head' }, e('span', { className: 'section-label' }, 'Indexes'), e('button', { className: 'mini', onClick: addIndex }, '+ Add')),
      (data.indexes || []).length === 0 ? e('p', { className: 'muted' }, 'No explicit indexes.') : null,
      (data.indexes || []).map((ix, i) => e('div', { key: i, className: 'index-row' },
        e('input', { className: 'idx-cols', placeholder: 'cols (comma)', value: (ix.columns || []).join(','), onChange: (ev) => setIndex(i, { columns: ev.target.value.split(',').map((s) => s.trim()).filter(Boolean) }) }),
        e('select', { value: ix.type || 'btree', onChange: (ev) => setIndex(i, { type: ev.target.value }) }, INDEX_TYPES.map((t) => e('option', { key: t, value: t }, t))),
        e('label', { className: 'idx-uniq', title: 'unique' }, e('input', { type: 'checkbox', checked: !!ix.unique, onChange: (ev) => setIndex(i, { unique: ev.target.checked }) }), 'U'),
        e('button', { className: 'icon-btn danger', onClick: () => removeIndex(i) }, '×')))),
    e('div', { className: 'side-foot' }, e('button', { onClick: onClose }, 'Done'))
  );
}

// ---------------------------------------------------------------------------
// Zoom / ERD / Code / Versions
// ---------------------------------------------------------------------------

function ZoomControls() {
  const rf = useReactFlow();
  return e(Panel, { position: 'top-right', className: 'zoom-panel' },
    e('button', { title: 'Zoom in', onClick: () => rf.zoomIn() }, '+'),
    e('button', { title: 'Zoom out', onClick: () => rf.zoomOut() }, '–'),
    e('button', { title: 'Fit to view', onClick: () => rf.fitView({ padding: 0.2 }) }, 'Fit'),
    e('button', { title: 'Reset to 100%', onClick: () => rf.zoomTo(1) }, '100%'));
}

function ErdView({ getSchema }) {
  const ref = useRef(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const data = await api('/export', { schema: getSchema(), type: 'mermaid' });
      if (cancelled || !ref.current) return;
      try {
        if (window.mermaid) { const { svg } = await window.mermaid.render('erd-graph', data.content); ref.current.innerHTML = svg; }
        else { ref.current.textContent = data.content; }
      } catch (err) { ref.current.innerHTML = `<pre>${data.content}</pre>`; }
    })();
    return () => { cancelled = true; };
  }, []);
  return e('div', { className: 'erd', ref });
}

function CodeView({ getSchema, tables, frameworks }) {
  const [exportType, setExportType] = useState('sql');
  const [modelFw, setModelFw] = useState('laravel');
  const [crudFw, setCrudFw] = useState('laravel');
  const [table, setTable] = useState(tables[0] || '');
  const [output, setOutput] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async (path, body, label) => {
    setBusy(true);
    try { const data = await api(path, { schema: getSchema(), ...body }); setOutput({ label, content: data.content || data.error || '' }); }
    finally { setBusy(false); }
  };
  const fw = frameworks || { export: [exportType], model: [modelFw], crud: [crudFw] };
  return e(
    'div', { className: 'code-view' },
    e('div', { className: 'code-controls' },
      e('div', { className: 'control-group' }, e('label', null, 'Export schema'),
        e('select', { value: exportType, onChange: (ev) => setExportType(ev.target.value) }, (fw.export || []).map((t) => e('option', { key: t, value: t }, t))),
        e('button', { disabled: busy, onClick: () => run('/export', { type: exportType }, `Export · ${exportType}`) }, 'Generate')),
      e('div', { className: 'control-group' }, e('label', null, 'Table'),
        e('select', { value: table, onChange: (ev) => setTable(ev.target.value) }, tables.map((t) => e('option', { key: t, value: t }, t)))),
      e('div', { className: 'control-group' }, e('label', null, 'Model'),
        e('select', { value: modelFw, onChange: (ev) => setModelFw(ev.target.value) }, (fw.model || []).map((t) => e('option', { key: t, value: t }, t))),
        e('button', { disabled: busy || !table, onClick: () => run('/generate/model', { framework: modelFw, table }, `Model · ${modelFw} · ${table}`) }, 'Generate Model')),
      e('div', { className: 'control-group' }, e('label', null, 'CRUD'),
        e('select', { value: crudFw, onChange: (ev) => setCrudFw(ev.target.value) }, (fw.crud || []).map((t) => e('option', { key: t, value: t }, t))),
        e('button', { disabled: busy || !table, onClick: () => run('/generate/crud', { framework: crudFw, table }, `CRUD · ${crudFw} · ${table}`) }, 'Generate CRUD'))),
    output && e('div', { className: 'export-panel' },
      e('div', { className: 'export-head' }, e('strong', null, output.label), e('button', { onClick: () => navigator.clipboard.writeText(output.content) }, 'Copy')),
      e('pre', null, e('code', null, output.content)))
  );
}

function VersionsView({ versions, onSave, getSchema }) {
  const [a, setA] = useState('current');
  const [b, setB] = useState('current');
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const schemaAt = (key) => (key === 'current' ? getSchema() : (versions[Number(key)] && versions[Number(key)].schema));
  const options = [...versions.map((v, i) => ({ key: String(i), label: v.label })), { key: 'current', label: '(current working schema)' }];
  const compare = async () => {
    const oldS = schemaAt(a); const newS = schemaAt(b);
    if (!oldS || !newS) return;
    setBusy(true);
    try { setResult(await api('/compare', { old: oldS, new: newS })); } finally { setBusy(false); }
  };
  return e(
    'div', { className: 'code-view' },
    e('div', { className: 'code-controls' },
      e('button', { onClick: onSave }, '💾 Save current as version'),
      e('div', { className: 'control-group' }, e('label', null, 'Base (old)'), selectEl(a, setA, options)),
      e('div', { className: 'control-group' }, e('label', null, 'Target (new)'), selectEl(b, setB, options)),
      e('button', { disabled: busy, onClick: compare }, 'Compare → migration')),
    e('div', { className: 'versions-list' },
      versions.length ? versions.map((v, i) => e('div', { key: i, className: 'version-item' }, e('span', null, v.label), e('span', { className: 'muted' }, `${(v.schema.tables || []).length} tables`)))
        : e('p', { className: 'muted' }, 'No saved versions yet — "Save current" to start a history, then Compare to generate a migration.')),
    result && e('div', { className: 'export-panel' },
      e('div', { className: 'export-head' }, e('strong', null, `Diff · ${result.diff.summary}`), e('button', { onClick: () => navigator.clipboard.writeText(result.migration) }, 'Copy migration')),
      e('pre', null, e('code', null, result.migration)))
  );
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

function App() {
  const [prompt, setPrompt] = useState('Build a clothing store');
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [schema, setSchema] = useState(null);
  const [enums, setEnums] = useState([]);
  const [versions, setVersions] = useState([]);
  const [validation, setValidation] = useState(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState('design');
  const [frameworks, setFrameworks] = useState(null);
  const [presets, setPresets] = useState([]);
  const [selection, setSelection] = useState(null);
  const [query, setQuery] = useState('');
  const [past, setPast] = useState([]);
  const [future, setFuture] = useState([]);

  const nodeTypes = useMemo(() => ({ table: TableNode }), []);

  useEffect(() => {
    fetch('/frameworks').then((r) => r.json()).then(setFrameworks).catch(() => {});
    fetch('/field-presets').then((r) => r.json()).then((d) => setPresets(d.presets || [])).catch(() => {});
  }, []);

  // ---- history (#10) ----
  const snapshot = () => { setPast((p) => [...p.slice(-49), { nodes, edges, enums }]); setFuture([]); };
  const undo = () => {
    if (!past.length) return;
    const prev = past[past.length - 1];
    setFuture((f) => [{ nodes, edges, enums }, ...f]);
    setPast((p) => p.slice(0, -1));
    setNodes(prev.nodes); setEdges(prev.edges); setEnums(prev.enums || []); setSelection(null);
  };
  const redo = () => {
    if (!future.length) return;
    const next = future[0];
    setPast((p) => [...p, { nodes, edges, enums }]);
    setFuture((f) => f.slice(1));
    setNodes(next.nodes); setEdges(next.edges); setEnums(next.enums || []); setSelection(null);
  };
  const undoRef = useRef(undo); undoRef.current = undo;
  const redoRef = useRef(redo); redoRef.current = redo;
  useEffect(() => {
    const onKey = (ev) => {
      const tag = (ev.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      const ctrl = ev.ctrlKey || ev.metaKey;
      if (ctrl && ev.key.toLowerCase() === 'z' && !ev.shiftKey) { ev.preventDefault(); undoRef.current(); }
      else if (ctrl && (ev.key.toLowerCase() === 'y' || (ev.key.toLowerCase() === 'z' && ev.shiftKey))) { ev.preventDefault(); redoRef.current(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // ---- ReactFlow change handlers ----
  const onNodesChange = useCallback((c) => setNodes((nds) => applyNodeChanges(c, nds)), []);
  const onEdgesChange = useCallback((c) => setEdges((eds) => applyEdgeChanges(c, eds)), []);
  const onConnect = useCallback((conn) => { snapshot(); setEdges((eds) =>
    addEdge({ ...conn, animated: true, label: '∞:1', data: { relation: { from_table: conn.source, to_table: conn.target, type: 'many_to_one', on_delete: 'cascade', on_update: 'cascade' } } }, eds)); }, [nodes, edges, enums]);

  const updateNode = (tableId, fn) => setNodes((nds) => nds.map((n) => (n.id === tableId ? fn(n) : n)));

  // ---- column actions ----
  const addField = (tableId) => {
    snapshot();
    let newIdx = 0;
    setNodes((nds) => nds.map((n) => {
      if (n.id !== tableId) return n;
      newIdx = n.data.fields.length;
      return { ...n, data: { ...n.data, fields: [...n.data.fields, { name: 'new_column', type: 'varchar', nullable: true }] } };
    }));
    setSelection({ kind: 'column', tableId, idx: newIdx });
  };
  const deleteColumn = (tableId, idx) => { snapshot(); updateNode(tableId, (n) => ({ ...n, data: { ...n.data, fields: n.data.fields.filter((_, i) => i !== idx) } })); setSelection(null); };
  const selectColumn = (tableId, idx) => setSelection({ kind: 'column', tableId, idx });

  // ---- table actions ----
  const renameTable = (tableId) => {
    const name = window.prompt('New table name?', tableId);
    if (!name || name === tableId) return;
    snapshot();
    setNodes((nds) => nds.map((n) => (n.id === tableId ? { ...n, id: name, data: { ...n.data, id: name, label: name } } : n)));
    setEdges((eds) => eds.map((ed) => ({ ...ed, source: ed.source === tableId ? name : ed.source, target: ed.target === tableId ? name : ed.target })));
  };
  const deleteTable = (tableId) => {
    if (!window.confirm(`Delete table "${tableId}"?`)) return;
    snapshot();
    setNodes((nds) => nds.filter((n) => n.id !== tableId));
    setEdges((eds) => eds.filter((ed) => ed.source !== tableId && ed.target !== tableId));
    setSelection(null);
  };
  const duplicateTable = (tableId) => {
    snapshot();
    setNodes((nds) => {
      const src = nds.find((n) => n.id === tableId);
      if (!src) return nds;
      return [...nds, makeNode({ name: `${tableId}_copy`, fields: JSON.parse(JSON.stringify(src.data.fields)), indexes: JSON.parse(JSON.stringify(src.data.indexes || [])), soft_delete: src.data.soft_delete, timestamps: src.data.timestamps, group: src.data.group, description: src.data.description }, { x: src.position.x + 40, y: src.position.y + 40 })];
    });
  };
  const tableSettings = (tableId) => setSelection({ kind: 'table', tableId });
  const onEdgeClick = useCallback((_ev, edge) => setSelection({ kind: 'relation', edgeId: edge.id }), []);

  const addEnum = () => {
    const name = window.prompt('Enum name? (e.g. order_status)');
    if (!name) return;
    const raw = window.prompt('Values, comma-separated? (e.g. pending, paid, shipped)', 'pending, active, done');
    if (raw === null) return;
    const values = raw.split(',').map((s) => s.trim()).filter(Boolean);
    snapshot();
    setEnums((es) => (es.some((x) => x.name === name) ? es : [...es, { name, values }]));
  };

  const callbacks = { onAddField: addField, onDeleteColumn: deleteColumn, onSelectColumn: selectColumn, onRenameTable: renameTable, onDeleteTable: deleteTable, onDuplicateTable: duplicateTable, onTableSettings: tableSettings };

  const q = query.trim().toLowerCase();
  const displayNodes = useMemo(() => nodes.map((n) => {
    const match = !q || n.data.label.toLowerCase().includes(q);
    return {
      ...n,
      style: { opacity: match ? 1 : 0.2 },
      className: q && match ? 'search-hit' : '',
      data: { ...n.data, ...callbacks, groupColor: groupColor(n.data.group), selectedField: selection && selection.kind === 'column' && selection.tableId === n.id ? selection.idx : null },
    };
  }), [nodes, selection, q, presets]);
  const displayEdges = useMemo(() => edges.map((ed) => {
    const match = !q || ed.source.toLowerCase().includes(q) || ed.target.toLowerCase().includes(q);
    return { ...ed, style: { opacity: match ? 1 : 0.15 } };
  }), [edges, q]);

  // ---- toolbar actions ----
  const generate = async () => {
    setBusy(true);
    try {
      const data = await api('/design', { feature_request: prompt });
      const s = data.database_schema;
      snapshot();
      setSchema(s); setEnums(s.enums || []);
      const g = schemaToGraph(s);
      setNodes(g.nodes); setEdges(g.edges);
      setValidation(s.validation); setSelection(null);
    } finally { setBusy(false); }
  };
  const addTable = () => {
    const name = window.prompt('Table name?', 'new_table');
    if (!name) return;
    snapshot();
    setNodes((nds) => [...nds, makeNode({ name, fields: [{ name: 'id', type: 'bigint', primary_key: true, auto_increment: true, nullable: false }] }, { x: 120 + Math.random() * 300, y: 120 + Math.random() * 300 })]);
  };
  const validate = async () => { const data = await api('/validate', { schema: graphToSchema(nodes, edges, schema, enums) }); setValidation(data.validation); };
  const saveVersion = () => setVersions((vs) => [...vs, { label: `v${vs.length + 1} · ${new Date().toLocaleTimeString()}`, schema: graphToSchema(nodes, edges, schema, enums) }]);

  const getSchema = useCallback(() => graphToSchema(nodes, edges, schema, enums), [nodes, edges, schema, enums]);
  const tableNames = nodes.map((n) => n.data.label);

  // ---- selection editors ----
  const editColumn = (next) => { snapshot(); updateNode(selection.tableId, (n) => ({ ...n, data: { ...n.data, fields: n.data.fields.map((f, i) => (i === selection.idx ? next : f)) } })); };
  const selectedField = selection && selection.kind === 'column' ? (nodes.find((n) => n.id === selection.tableId)?.data.fields[selection.idx]) : null;
  const editRelation = (next) => { snapshot(); setEdges((eds) => eds.map((ed) => (ed.id === selection.edgeId ? { ...ed, label: CARDINALITY[next.type] || next.type, data: { ...ed.data, relation: next } } : ed))); };
  const selectedEdge = selection && selection.kind === 'relation' ? edges.find((ed) => ed.id === selection.edgeId) : null;
  const editTable = (nextData) => { snapshot(); updateNode(selection.tableId, (n) => ({ ...n, data: { ...n.data, ...nextData } })); };
  const selectedTable = selection && selection.kind === 'table' ? nodes.find((n) => n.id === selection.tableId) : null;

  const groups = useMemo(() => {
    const set = {};
    nodes.forEach((n) => { if (n.data.group) set[n.data.group] = groupColor(n.data.group); });
    return Object.entries(set);
  }, [nodes]);

  return e(
    'div', { className: 'designer' },
    e('div', { className: 'toolbar' },
      e('input', { className: 'prompt', value: prompt, placeholder: 'Describe your database…', onChange: (ev) => setPrompt(ev.target.value) }),
      e('button', { onClick: generate, disabled: busy }, busy ? 'Generating…' : 'Generate Schema'),
      e('button', { onClick: addTable }, '+ Table'),
      e('button', { onClick: validate, disabled: !nodes.length }, 'Validate'),
      e('button', { className: 'ghost', onClick: undo, disabled: !past.length, title: 'Undo (Ctrl+Z)' }, '↶ Undo'),
      e('button', { className: 'ghost', onClick: redo, disabled: !future.length, title: 'Redo (Ctrl+Y)' }, '↷ Redo'),
      e('input', { className: 'search', value: query, placeholder: '🔍 Filter tables…', onChange: (ev) => setQuery(ev.target.value) }),
      e('div', { className: 'tabs' }, ['design', 'erd', 'code', 'versions'].map((t) =>
        e('button', { key: t, className: 'tab' + (tab === t ? ' active' : ''), onClick: () => setTab(t) },
          t === 'design' ? 'Design' : t === 'erd' ? 'ERD' : t === 'code' ? 'Code' : 'Versions')))),
    validation && e('div', { className: 'validation ' + (validation.valid ? 'ok' : 'bad') },
      validation.valid ? '✅ Schema is valid' : `⚠️ ${validation.errors.length} error(s)`,
      validation.warnings && validation.warnings.length ? ` · ${validation.warnings.length} warning(s)` : '',
      !validation.valid && validation.errors.length ? e('ul', null, validation.errors.map((er, i) => e('li', { key: i }, er))) : null),
    groups.length ? e('div', { className: 'group-legend' }, groups.map(([name, color]) => e('span', { key: name, className: 'group-chip' }, e('i', { style: { background: color } }), name))) : null,
    e('div', { className: 'workspace' },
      e('div', { className: 'main' },
        tab === 'design' && e('div', { className: 'canvas' },
          e(ReactFlow, { nodes: displayNodes, edges: displayEdges, onNodesChange, onEdgesChange, onConnect, onEdgeClick, nodeTypes, fitView: true, minZoom: 0.2, maxZoom: 2 },
            e(Background, null), e(Controls, null), e(MiniMap, { nodeColor: (n) => groupColor(n.data && n.data.group) || '#4f46e5' }), e(ZoomControls, null))),
        tab === 'erd' && e(ErdView, { getSchema, key: nodes.length + ':' + edges.length }),
        tab === 'code' && e(CodeView, { getSchema, tables: tableNames, frameworks }),
        tab === 'versions' && e(VersionsView, { versions, onSave: saveVersion, getSchema })),
      tab === 'design' && selectedField &&
        e(ColumnEditor, { field: selectedField, presets, enums, onChange: editColumn, onClose: () => setSelection(null), onDelete: () => deleteColumn(selection.tableId, selection.idx), onAddEnum: addEnum }),
      tab === 'design' && selectedEdge &&
        e(RelationEditor, { relation: selectedEdge.data.relation, onChange: editRelation, onClose: () => setSelection(null), onDelete: () => { snapshot(); setEdges((eds) => eds.filter((ed) => ed.id !== selection.edgeId)); setSelection(null); } }),
      tab === 'design' && selectedTable &&
        e(TableEditor, { table: selectedTable, onChange: editTable, onClose: () => setSelection(null) })
    )
  );
}

if (window.mermaid) { window.mermaid.initialize({ startOnLoad: false, theme: 'neutral' }); }
createRoot(document.getElementById('root')).render(e(App));
