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
  document.getElementById('__sf-flow-highlight')?.remove();
  document.getElementById('__sf-flow-element-info')?.remove();

  const style = document.createElement('style');
  style.id = '__sf-flow-picker-style';
  style.textContent = `
    .__sf-flow-hover { cursor: crosshair !important; }
    #__sf-flow-banner { position: fixed; z-index: 2147483647; left: 16px; top: 16px;
      padding: 10px 14px; background: #172b4d; color: white; border-radius: 6px;
      font: 14px sans-serif; box-shadow: 0 2px 8px #555; pointer-events: none; }
    #__sf-flow-highlight { position: fixed; z-index: 2147483646; box-sizing: border-box;
      border: 4px solid #ff2d8d; box-shadow: inset 0 0 0 1px white;
      pointer-events: none; }
    #__sf-flow-element-info { position: fixed; z-index: 2147483647;
      max-width: min(520px, calc(100vw - 32px)); padding: 10px 14px;
      background: rgba(23, 43, 77, .96); color: white; border-radius: 6px;
      font: 13px/1.5 sans-serif; box-shadow: 0 2px 8px #555; pointer-events: none;
      white-space: pre-wrap; }
  `;
  document.head.appendChild(style);
  const banner = document.createElement('div');
  banner.id = '__sf-flow-banner';
  banner.textContent = __WAITING_TEXT__;
  document.body.appendChild(banner);
  const highlight = document.createElement('div');
  highlight.id = '__sf-flow-highlight';
  highlight.hidden = true;
  document.body.appendChild(highlight);
  const elementInfo = document.createElement('div');
  elementInfo.id = '__sf-flow-element-info';
  elementInfo.hidden = true;
  document.body.appendChild(elementInfo);

  let hovered = null;
  let pointerX = 0;
  let pointerY = 0;
  const actionableSelector = [
    'button', 'a[href]', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="checkbox"]',
    '[role="radio"]', '[role="textbox"]', '[role="combobox"]',
    '[contenteditable="true"]'
  ].join(',');

  const eventElement = (event) => {
    const elements = event.composedPath().filter(item => item instanceof Element);
    return elements.find(element =>
      ![banner, highlight, elementInfo].includes(element) &&
      element.matches(actionableSelector) &&
      !element.hasAttribute('disabled') &&
      element.getAttribute('aria-disabled') !== 'true'
    ) || elements.find(element => ![banner, highlight, elementInfo].includes(element)) || null;
  };

  const updateFeedback = () => {
    if (!hovered || !hovered.isConnected) return;
    const rect = hovered.getBoundingClientRect();
    const left = Math.max(0, rect.left);
    const top = Math.max(0, rect.top);
    const right = Math.min(window.innerWidth, rect.right);
    const bottom = Math.min(window.innerHeight, rect.bottom);
    highlight.hidden = right <= left || bottom <= top;
    if (!highlight.hidden) {
      Object.assign(highlight.style, {
        left: `${left}px`, top: `${top}px`,
        width: `${right - left}px`, height: `${bottom - top}px`
      });
    }
    const viewWidth = hovered.clientWidth;
    const viewHeight = hovered.clientHeight;
    const fullWidth = hovered.scrollWidth;
    const fullHeight = hovered.scrollHeight;
    const horizontal = Math.max(0, fullWidth - viewWidth);
    const vertical = Math.max(0, fullHeight - viewHeight);
    const directions = [horizontal ? `↔ ${horizontal}px` : '', vertical ? `↕ ${vertical}px` : '']
      .filter(Boolean).join('  ') || 'NO SCROLL';
    const clipped = [
      rect.left < 0 ? '←' : '', rect.right > window.innerWidth ? '→' : '',
      rect.top < 0 ? '↑' : '', rect.bottom > window.innerHeight ? '↓' : ''
    ].filter(Boolean).join('');
    elementInfo.textContent = [
      `<${hovered.localName}>${hovered.id ? `#${hovered.id}` : ''}`,
      `VIEW ${viewWidth} × ${viewHeight}   CONTENT ${fullWidth} × ${fullHeight}`,
      `${directions}${clipped ? `   OUTSIDE ${clipped}` : ''}`
    ].join('\n');
    // 情報パネルはポインターと反対側へ置き、選択対象を隠さない。
    elementInfo.style.left = pointerX > window.innerWidth / 2 ? '16px' : 'auto';
    elementInfo.style.right = pointerX > window.innerWidth / 2 ? 'auto' : '16px';
    elementInfo.style.top = pointerY > window.innerHeight / 2 ? '16px' : 'auto';
    elementInfo.style.bottom = pointerY > window.innerHeight / 2 ? 'auto' : '16px';
    elementInfo.hidden = false;
  };

  const clean = () => {
    if (hovered) hovered.classList.remove('__sf-flow-hover');
    document.removeEventListener('mouseover', over, true);
    document.removeEventListener('click', click, true);
    document.removeEventListener('keydown', key, true);
    document.removeEventListener('keydown', activate, true);
    document.removeEventListener('scroll', updateFeedback, true);
    banner.remove();
    highlight.remove();
    elementInfo.remove();
    style.remove();
    window.__webFlowPickerCleanup = null;
  };
  window.__webFlowPickerCleanup = clean;

  const over = (event) => {
    const element = eventElement(event);
    if (!element) return;
    pointerX = event.clientX;
    pointerY = event.clientY;
    if (hovered && hovered !== element) hovered.classList.remove('__sf-flow-hover');
    hovered = element;
    hovered.classList.add('__sf-flow-hover');
    updateFeedback();
  };
  const esc = (value) => CSS.escape(String(value));
  const cssCandidate = (element, stable = false) => {
    if (element.id && !/\d{4,}/.test(element.id)) return `#${esc(element.id)}`;
    const attributes = stable
      ? ['data-target-selection-name', 'data-testid', 'data-id', 'name', 'role']
      : ['data-target-selection-name', 'data-testid', 'data-id', 'name',
          'aria-label', 'placeholder', 'title'];
    for (const attr of attributes) {
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
  const elementTextCandidates = (element) => [...new Set([
    element.innerText || '',
    element.textContent || '',
    element.value || ''
  ].map(value => String(value).trim().replace(/\s+/g, ' '))
    .filter(value => value && value.length <= 120))];
  const xpathCandidate = (element, allowText = true) => {
    const root = element.getRootNode();
    if (root instanceof ShadowRoot) return '';
    const tag = element.tagName.toLowerCase();
    const unique = (xpath) => xpathCount(xpath) === 1 ? xpath : '';
    const locatorAttributes = allowText
      ? ['data-target-selection-name', 'data-testid', 'data-id', 'name',
          'aria-label', 'placeholder', 'title']
      : ['data-target-selection-name', 'data-testid', 'data-id', 'name', 'role'];

    // 短く読みやすい定位を優先する。属性はアプリ固有の識別子を、
    // アクセシビリティ／表示用の文言より先に評価する。
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) {
      const candidate = unique(`//*[@id=${xpathLiteral(id)}]`);
      if (candidate) return candidate;
    }
    for (const attr of locatorAttributes) {
      const value = element.getAttribute(attr);
      if (!value) continue;
      const candidate = unique(`//${tag}[@${attr}=${xpathLiteral(value)}]`);
      if (candidate) return candidate;
    }
    if (allowText) {
      for (const exactText of elementTextCandidates(element)) {
        const candidate = unique(
          `//${tag}[normalize-space(.)=${xpathLiteral(exactText)}]`
        );
        if (candidate) return candidate;
      }
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
      for (const attr of locatorAttributes) {
        const value = node.getAttribute(attr);
        if (!value) continue;
        const candidate = unique(`//${nodeTag}[@${attr}=${xpathLiteral(value)}]`);
        if (candidate) return candidate;
      }
      if (allowText) {
        for (const nodeText of elementTextCandidates(node)) {
          const candidate = unique(`//${nodeTag}[normalize-space(.)=${xpathLiteral(nodeText)}]`);
          if (candidate) return candidate;
        }
      }
      return '';
    };
    const scopedTargetCandidates = (node) => {
      const nodeTag = node.tagName.toLowerCase();
      const candidates = [];
      // ボタンやリンクは表示文言を最優先し、入力項目などは安定属性を優先する。
      const textCandidates = allowText ? elementTextCandidates(node).map(
        text => `${nodeTag}[normalize-space(.)=${xpathLiteral(text)}]`
      ) : [];
      if (['button', 'a'].includes(nodeTag)) candidates.push(...textCandidates);
      for (const attr of locatorAttributes) {
        const value = node.getAttribute(attr);
        if (value) candidates.push(`${nodeTag}[@${attr}=${xpathLiteral(value)}]`);
      }
      if (!['button', 'a'].includes(nodeTag)) candidates.push(...textCandidates);
      candidates.push(nodeTag);
      return [...new Set(candidates)];
    };
    const resolvesTarget = (xpath) => {
      const result = document.evaluate(
        xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
      );
      return result.snapshotLength === 1 && result.snapshotItem(0) === element;
    };
    const semanticAnchoredCandidate = () => {
      const semanticSelector = [
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'legend', 'label',
        '[role="heading"]'
      ].join(',');
      let scope = element.parentElement;
      for (let depth = 0; scope && depth < 7; depth += 1, scope = scope.parentElement) {
        const scopeTag = scope.tagName.toLowerCase();
        const anchors = Array.from(scope.querySelectorAll(semanticSelector))
          .filter(anchor => anchor !== element && (anchor.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING))
          .reverse();
        for (const anchor of anchors) {
          const anchorTag = anchor.tagName.toLowerCase();
          for (const text of elementTextCandidates(anchor)) {
            const anchorPath = `//${anchorTag}[normalize-space(.)=${xpathLiteral(text)}]`;
            if (xpathCount(anchorPath) !== 1) continue;
            for (const targetCandidate of targetCandidates) {
              const relationshipCandidates = [
                `${anchorPath}/following-sibling::${targetCandidate}`,
                `${anchorPath}/following-sibling::*[1]//${targetCandidate}`,
                `${anchorPath}/parent::*//${targetCandidate}`,
                `//${scopeTag}[.//${anchorTag}[normalize-space(.)=${xpathLiteral(text)}]]//${targetCandidate}`
              ];
              for (const candidate of relationshipCandidates) {
                if (resolvesTarget(candidate)) return candidate;
              }
            }
          }
        }
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
    const plainParts = [];
    const targetCandidates = scopedTargetCandidates(element);
    // 同名要素が複数ある場合は、位置番号より先に近隣の見出しを意味的な起点として利用する。
    const semanticCandidate = semanticAnchoredCandidate();
    if (semanticCandidate) return semanticCandidate;
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      parts.unshift(segment(current));
      plainParts.unshift(current.tagName.toLowerCase());
      const anchor = anchorCandidate(current);
      if (anchor) {
        const descendants = parts.slice(1);
        if (!descendants.length) return anchor;
        // 全体では重複する文字や属性も、一意な親要素の範囲内で再評価する。
        for (const targetCandidate of targetCandidates) {
          const scoped = unique(`${anchor}//${targetCandidate}`);
          if (scoped) return scoped;
        }
        // 位置番号を付けない短い構造パスを先に試す。
        const plainDescendants = plainParts.slice(1);
        for (let length = 1; length <= plainDescendants.length; length += 1) {
          const suffix = plainDescendants.slice(-length).join('/');
          const anchoredPlain = unique(`${anchor}//${suffix}`);
          if (anchoredPlain) return anchoredPlain;
        }
        // 最後に位置番号を含むパスを末端から一段ずつ追加する。
        for (let length = 1; length <= descendants.length; length += 1) {
          const suffix = descendants.slice(-length).join('/');
          const anchoredRelative = unique(`${anchor}//${suffix}`);
          if (anchoredRelative) return anchoredRelative;
        }
        return `${anchor}/${descendants.join('/')}`;
      }
      const plainRelative = unique(`//${plainParts.join('/')}`);
      if (plainRelative) return plainRelative;
      // 単一タグの位置指定（例: //input[2]）は、別の親配下にも一致するため使用しない。
      // 親要素を含む構造パスになるまで探索を続ける。
      if (parts.length > 1) {
        const relative = unique(`//${parts.join('/')}`);
        if (relative) return relative;
      }
      current = current.parentElement;
    }
    return `/${parts.join('/')}`;
  };
  const elementResult = (element) => {
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
    return {
      tag, role, name, label,
      input_type: (element.getAttribute('type') || '').toLowerCase(),
      content_editable: element.isContentEditable ? 'true' : 'false',
      placeholder: element.getAttribute('placeholder') || '',
      text,
      css: cssCandidate(element),
      stable_css: cssCandidate(element, true),
      xpath: xpathCandidate(element),
      stable_xpath: xpathCandidate(element, false)
    };
  };
  const click = (event) => {
    const element = eventElement(event);
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    window.__sfFlowPicked = elementResult(element);
    clean();
  };
  const key = (event) => {
    if (event.key === 'Escape') {
      window.__sfFlowPicked = {cancelled: true};
      clean();
    } else if (event.key === 'Enter' && hovered) {
      event.preventDefault();
      event.stopPropagation();
      window.__sfFlowPicked = elementResult(hovered);
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
    document.addEventListener('scroll', updateFeedback, true);
  };
  document.addEventListener('keydown', activate, true);
}
"""


def picker_script(
    run_id: str, waiting_text: str, active_text: str,
) -> str:
    """表示言語の案内文を安全に埋め込んだ選択スクリプトを返す。"""
    return (
        _PICKER_SCRIPT
        .replace('__RUN_ID__', json.dumps(run_id))
        .replace('__WAITING_TEXT__', json.dumps(waiting_text, ensure_ascii=False))
        .replace('__ACTIVE_TEXT__', json.dumps(active_text, ensure_ascii=False))
    )
