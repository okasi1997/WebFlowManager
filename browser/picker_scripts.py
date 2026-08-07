"""要素選択時に各フレームへ注入する JavaScript を生成する。"""
from __future__ import annotations

import json


_PICKER_SCRIPT = r"""
() => {
  if (typeof window.__webFlowPickerCleanup === 'function') {
    window.__webFlowPickerCleanup();
  }
  window.__webFlowPickerRunId = __RUN_ID__;
  window.__sfFlowPicked = null;
  document.getElementById('__sf-flow-picker-style')?.remove();
  document.getElementById('__sf-flow-banner')?.remove();

  const style = document.createElement('style');
  style.id = '__sf-flow-picker-style';
  style.textContent = `
    .__sf-flow-hover { outline: 3px solid #e91e63 !important; cursor: crosshair !important; }
    #__sf-flow-banner { position: fixed; z-index: 2147483647; left: 16px; top: 16px;
      padding: 10px 14px; background: #172b4d; color: white; border-radius: 6px;
      font: 14px sans-serif; box-shadow: 0 2px 8px #555; pointer-events: none; }
  `;
  document.head.appendChild(style);
  const banner = document.createElement('div');
  banner.id = '__sf-flow-banner';
  banner.textContent = __WAITING_TEXT__;
  document.body.appendChild(banner);

  let hovered = null;
  const actionableSelector = [
    'button', 'a[href]', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="checkbox"]',
    '[role="radio"]', '[role="textbox"]', '[role="combobox"]',
    '[contenteditable="true"]'
  ].join(',');

  const eventElement = (event) => {
    const elements = event.composedPath().filter(item => item instanceof Element);
    return elements.find(element =>
      element !== banner &&
      element.matches(actionableSelector) &&
      !element.hasAttribute('disabled') &&
      element.getAttribute('aria-disabled') !== 'true'
    ) || elements.find(element => element !== banner) || null;
  };

  const clean = () => {
    if (hovered) hovered.classList.remove('__sf-flow-hover');
    document.removeEventListener('mouseover', over, true);
    document.removeEventListener('click', click, true);
    document.removeEventListener('keydown', key, true);
    document.removeEventListener('keydown', activate, true);
    banner.remove();
    style.remove();
    window.__webFlowPickerCleanup = null;
  };
  window.__webFlowPickerCleanup = clean;

  const over = (event) => {
    const element = eventElement(event);
    if (!element) return;
    if (hovered && hovered !== element) hovered.classList.remove('__sf-flow-hover');
    hovered = element;
    hovered.classList.add('__sf-flow-hover');
  };
  const esc = (value) => CSS.escape(String(value));
  const cssCandidate = (element) => {
    if (element.id && !/\d{4,}/.test(element.id)) return `#${esc(element.id)}`;
    for (const attr of [
      'data-target-selection-name', 'data-id', 'name',
      'aria-label', 'placeholder', 'title'
    ]) {
      const value = element.getAttribute(attr);
      if (value) return `${element.tagName.toLowerCase()}[${attr}="${esc(value)}"]`;
    }
    return element.tagName.toLowerCase();
  };
  const xpathLiteral = (value) => {
    value = String(value);
    if (!value.includes("'")) return `'${value}'`;
    if (!value.includes('"')) return `"${value}"`;
    return `concat('${value.split("'").join("',\"'\",'")}')`;
  };
  const xpathCount = (xpath) => document.evaluate(
    xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
  ).snapshotLength;
  const xpathCandidate = (element) => {
    const root = element.getRootNode();
    if (root instanceof ShadowRoot) return '';
    const tag = element.tagName.toLowerCase();
    const unique = (xpath) => xpathCount(xpath) === 1 ? xpath : '';

    // 短く読みやすい定位を優先する。属性はアプリ固有の識別子を、
    // アクセシビリティ／表示用の文言より先に評価する。
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) {
      const candidate = unique(`//*[@id=${xpathLiteral(id)}]`);
      if (candidate) return candidate;
    }
    for (const attr of [
      'data-target-selection-name', 'data-id', 'name',
      'aria-label', 'placeholder', 'title'
    ]) {
      const value = element.getAttribute(attr);
      if (!value) continue;
      const candidate = unique(`//${tag}[@${attr}=${xpathLiteral(value)}]`);
      if (candidate) return candidate;
    }
    const exactText = (element.innerText || '').trim().replace(/\s+/g, ' ');
    if (exactText && exactText.length <= 120) {
      const candidate = unique(
        `//${tag}[normalize-space(.)=${xpathLiteral(exactText)}]`
      );
      if (candidate) return candidate;
    }

    // 簡潔で一意な定位がない場合は、一意に特定できる親要素を起点とする
    // 最短パスへ切り替え、同名要素がある画面でも選択結果を保持する。
    const anchorCandidate = (node) => {
      const nodeTag = node.tagName.toLowerCase();
      const nodeId = node.getAttribute('id');
      if (nodeId && !/\d{4,}/.test(nodeId)) {
        const candidate = unique(`//*[@id=${xpathLiteral(nodeId)}]`);
        if (candidate) return candidate;
      }
      for (const attr of [
        'data-target-selection-name', 'data-id', 'name', 'role',
        'aria-label', 'placeholder', 'title'
      ]) {
        const value = node.getAttribute(attr);
        if (!value) continue;
        const candidate = unique(`//${nodeTag}[@${attr}=${xpathLiteral(value)}]`);
        if (candidate) return candidate;
      }
      const nodeText = (node.innerText || '').trim().replace(/\s+/g, ' ');
      if (nodeText && nodeText.length <= 120) {
        return unique(`//${nodeTag}[normalize-space(.)=${xpathLiteral(nodeText)}]`);
      }
      return '';
    };
    const segment = (node) => {
      const nodeTag = node.tagName.toLowerCase();
      if (!node.parentElement) return `${nodeTag}[1]`;
      const siblings = Array.from(node.parentElement.children).filter(
        sibling => sibling.tagName === node.tagName
      );
      return `${nodeTag}[${siblings.indexOf(node) + 1}]`;
    };
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      parts.unshift(segment(current));
      const anchor = anchorCandidate(current);
      if (anchor) {
        const descendants = parts.slice(1);
        if (!descendants.length) return anchor;
        // 一意な親要素の範囲内で末端から最短の子孫パスを試し、一意でなければ
        // 親要素を一段ずつ追加する。最後に完全な直接子パスを使用する。
        for (let length = 1; length <= descendants.length; length += 1) {
          const suffix = descendants.slice(-length).join('/');
          const anchoredRelative = unique(`${anchor}//${suffix}`);
          if (anchoredRelative) return anchoredRelative;
        }
        return `${anchor}/${descendants.join('/')}`;
      }
      const relative = unique(`//${parts.join('/')}`);
      if (relative) return relative;
      current = current.parentElement;
    }
    return `/${parts.join('/')}`;
  };
  const click = (event) => {
    const element = eventElement(event);
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    const tag = element.tagName.toLowerCase();
    const implicitRoles = {
      button: 'button',
      a: 'link',
      input: element.type === 'checkbox' ? 'checkbox' :
        element.type === 'radio' ? 'radio' : 'textbox',
      select: 'combobox',
      textarea: 'textbox'
    };
    const role = element.getAttribute('role') || implicitRoles[tag] || '';
    const aria = element.getAttribute('aria-label') || '';
    let label = '';
    if (element.labels && element.labels.length) label = element.labels[0].innerText.trim();
    if (!label && element.id) {
      const labelElement = document.querySelector(`label[for="${esc(element.id)}"]`);
      if (labelElement) label = labelElement.innerText.trim();
    }
    const text = (element.innerText || element.value || '')
      .trim().replace(/\s+/g, ' ').slice(0, 120);
    const name = aria || label || text || element.getAttribute('title') || '';
    window.__sfFlowPicked = {
      tag, role, name, label,
      placeholder: element.getAttribute('placeholder') || '',
      text, css: cssCandidate(element), xpath: xpathCandidate(element)
    };
    clean();
  };
  const key = (event) => {
    if (event.key === 'Escape') {
      window.__sfFlowPicked = {cancelled: true};
      clean();
    }
  };
  const activate = (event) => {
    if (event.key === 'Escape') {
      window.__sfFlowPicked = {cancelled: true};
      clean();
      return;
    }
    if (event.key !== 'F2') return;
    event.preventDefault();
    banner.textContent = __ACTIVE_TEXT__;
    document.removeEventListener('keydown', activate, true);
    document.addEventListener('mouseover', over, true);
    document.addEventListener('click', click, true);
    document.addEventListener('keydown', key, true);
  };
  document.addEventListener('keydown', activate, true);
}
"""


def picker_script(run_id: str, waiting_text: str, active_text: str) -> str:
    """表示言語の案内文を安全に埋め込んだ選択スクリプトを返す。"""
    return (
        _PICKER_SCRIPT
        .replace('__RUN_ID__', json.dumps(run_id))
        .replace('__WAITING_TEXT__', json.dumps(waiting_text, ensure_ascii=False))
        .replace('__ACTIVE_TEXT__', json.dumps(active_text, ensure_ascii=False))
    )
