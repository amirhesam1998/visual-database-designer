// TableNode — a React Flow custom node that renders one database table.
// Phase 6F enhancements: the table header carries rename/delete actions, each field row is clickable
// (opens the column editor) and shows a delete button, and an "+ Add column" affordance sits at the
// bottom. Connection handles on the left (target) and right (source) let the user draw relationships.
import React from 'react';
import { Handle, Position } from 'reactflow';

const e = React.createElement;

export default function TableNode({ id, data }) {
  const fields = data.fields || [];
  const stop = (fn) => (ev) => {
    ev.stopPropagation();
    fn && fn();
  };
  const headerStyle = data.groupColor ? { background: data.groupColor } : null;

  return e(
    'div',
    { className: 'table-node' },
    e(Handle, { type: 'target', position: Position.Left }),
    e(
      'div',
      { className: 'table-header', style: headerStyle },
      e(
        'span',
        { className: 'table-title' },
        e('strong', { title: 'Click a field to edit it' }, data.label),
        data.group ? e('span', { className: 'group-tag', title: `Group: ${data.group}` }, data.group) : null
      ),
      e(
        'div',
        { className: 'table-actions' },
        e('button', { className: 'icon-btn', title: 'Table settings (group, description, indexes)', onClick: stop(() => data.onTableSettings(id)) }, '⚙'),
        e('button', { className: 'icon-btn', title: 'Rename table', onClick: stop(() => data.onRenameTable(id)) }, '✎'),
        e('button', { className: 'icon-btn', title: 'Duplicate table', onClick: stop(() => data.onDuplicateTable(id)) }, '⧉'),
        e('button', { className: 'icon-btn danger', title: 'Delete table', onClick: stop(() => data.onDeleteTable(id)) }, '🗑')
      )
    ),
    e(
      'div',
      { className: 'table-fields' },
      fields.map((field, idx) =>
        e(
          'div',
          {
            key: idx,
            className:
              'field' +
              (field.primary_key ? ' pk' : '') +
              (data.selectedField === idx ? ' selected' : ''),
            title: 'Edit column',
            onClick: stop(() => data.onSelectColumn(id, idx)),
          },
          field.primary_key
            ? e('span', { className: 'pk-badge' }, 'PK')
            : field.type === 'foreign_id' || /_id$/.test(field.name)
            ? e('span', { className: 'fk-badge' }, 'FK')
            : null,
          e('span', { className: 'field-name' }, field.name),
          e('span', { className: 'type' }, field.type + (field.length ? `(${field.length})` : '')),
          field.unique ? e('span', { className: 'flag', title: 'unique' }, 'U') : null,
          field.indexed && !field.unique ? e('span', { className: 'flag', title: 'indexed' }, 'I') : null,
          e('button', { className: 'icon-btn danger field-del', title: 'Delete column', onClick: stop(() => data.onDeleteColumn(id, idx)) }, '×')
        )
      )
    ),
    e('button', { className: 'add-field-btn', title: 'Add column', onClick: stop(() => data.onAddField(id)) }, '+ Add column'),
    e(Handle, { type: 'source', position: Position.Right })
  );
}
