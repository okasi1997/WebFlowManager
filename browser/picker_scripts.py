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
    .__sf-flow-path-anchor { outline: 3px solid #00a3ff !important;
      outline-offset: -3px !important; }
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
  const activeText = __ACTIVE_TEXT__;
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
  let selecting = false;
  let selectionKey = '';
  const pathAnchors = [];
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
    if (!selecting || !hovered || !hovered.isConnected) return;
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
    pathAnchors.forEach(element => element.classList.remove('__sf-flow-path-anchor'));
    document.removeEventListener('mouseover', over, true);
    document.removeEventListener('click', choose, true);
    document.removeEventListener('keydown', key, true);
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
    if (selecting) {
      hovered.classList.add('__sf-flow-hover');
      updateFeedback();
    }
  };
  const choose = (event) => {
    if (!selecting) return;
    const element = eventElement(event);
    if (!element) return;
    event.preventDefault();
    event.stopPropagation();
    hovered = element;
    if (selectionKey === 'F1') {
      if (!pathAnchors.includes(element)) {
        pathAnchors.push(element);
        element.classList.add('__sf-flow-path-anchor');
      }
      leaveSelectionMode();
    } else if (selectionKey === 'F2') {
      finish(element);
    }
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
  const displayName = (element) => {
    const result = elementResult(element);
    return result.label || result.name || result.text || `<${result.tag}>`;
  };
  const pathScope = (element) => element.closest(
    'tr, li, section, article, fieldset, form, table'
  ) || element;
  const pathText = (element) => String(
    element.innerText || element.textContent || element.value || ''
  ).trim().replace(/\s+/g, ' ').slice(0, 120);
  const pathFragment = (element, textSource = element) => {
    const tag = element.tagName.toLowerCase();
    const text = pathText(textSource);
    if (text) return `${tag}[contains(normalize-space(.),${xpathLiteral(text)})]`;
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) return `${tag}[@id=${xpathLiteral(id)}]`;
    for (const name of ['data-testid', 'data-id', 'name', 'aria-label', 'role']) {
      const value = element.getAttribute(name);
      if (value) return `${tag}[@${name}=${xpathLiteral(value)}]`;
    }
    return tag;
  };
  const pathCondition = (element) => {
    const text = pathText(element);
    if (text) return `contains(normalize-space(.),${xpathLiteral(text)})`;
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) return `@id=${xpathLiteral(id)}`;
    for (const name of ['data-testid', 'data-id', 'name', 'aria-label', 'role']) {
      const value = element.getAttribute(name);
      if (value) return `@${name}=${xpathLiteral(value)}`;
    }
    return '';
  };
  const pathGroupFragment = (scope, anchors) => {
    const tag = scope.tagName.toLowerCase();
    const conditions = [...new Set(anchors.map(pathCondition).filter(Boolean))];
    return conditions.length ? `${tag}[${conditions.join(' and ')}]` : tag;
  };
  const targetFragment = (element) => {
    const tag = element.tagName.toLowerCase();
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) return `${tag}[@id=${xpathLiteral(id)}]`;
    for (const name of [
      'data-target-selection-name', 'data-testid', 'data-id', 'name',
      'aria-label', 'placeholder', 'type', 'role'
    ]) {
      const value = element.getAttribute(name);
      if (value) return `${tag}[@${name}=${xpathLiteral(value)}]`;
    }
    const text = pathText(element);
    return text ? `${tag}[contains(normalize-space(.),${xpathLiteral(text)})]` : tag;
  };
  const targetValue = (element) => {
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) return id;
    for (const name of [
      'data-target-selection-name', 'data-testid', 'data-id', 'name',
      'aria-label', 'placeholder', 'type', 'role'
    ]) {
      const value = element.getAttribute(name);
      if (value) return value;
    }
    return pathText(element);
  };
  const commonAnchorXPath = (anchors, target) => {
    if (!anchors.length) return '';
    const scopes = anchors.map(pathScope);
    const candidates = [];
    let candidate = scopes[0];
    while (candidate) {
      if (candidate.matches?.('tr, li, section, article, fieldset, form, table')) {
        candidates.push(candidate);
      }
      candidate = candidate.parentElement;
    }
    const common = candidates.find(scope =>
      anchors.every(anchor => scope.contains(anchor)) && scope.contains(target)
    );
    if (!common) return '';
    const conditions = [];
    anchors.forEach((anchor, index) => {
      const condition = pathCondition(anchor);
      if (!condition) return;
      if (scopes[index] === common) conditions.push(condition);
      else conditions.push(`.//${anchor.tagName.toLowerCase()}[${condition}]`);
    });
    const uniqueConditions = [...new Set(conditions)];
    const commonTag = common.tagName.toLowerCase();
    const commonFragment = uniqueConditions.length
      ? `${commonTag}[${uniqueConditions.join(' and ')}]` : commonTag;
    return `//${commonFragment}`;
  };
  const siblingScopeXPath = (anchors, target) => {
    const commonXPath = commonAnchorXPath(anchors, target);
    if (!commonXPath) return '';
    const scopes = anchors.map(pathScope);
    const candidates = [];
    let candidate = scopes[0];
    while (candidate) {
      if (candidate.matches?.('tr, li, section, article, fieldset, form, table')) {
        candidates.push(candidate);
      }
      candidate = candidate.parentElement;
    }
    const common = candidates.find(scope =>
      anchors.every(anchor => scope.contains(anchor)) && scope.contains(target)
    );
    if (!common) return '';
    const targetPart = targetFragment(target);
    const relativeXPath = `.//${targetPart}`;
    const matches = document.evaluate(
      relativeXPath, common, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
    );
    let targetIndex = -1;
    for (let index = 0; index < matches.snapshotLength; index += 1) {
      if (matches.snapshotItem(index) === target) {
        targetIndex = index;
        break;
      }
    }
    if (targetIndex < 0) return '';
    const xpath = `${commonXPath}//${targetPart}`;
    return matches.snapshotLength === 1 ? xpath : `(${xpath})[${targetIndex + 1}]`;
  };
  const scopeMatch = (element) => {
    const text = pathText(element);
    if (text) return {method: 'text_contains', attribute: '', value: text};
    const id = element.getAttribute('id');
    if (id && !/\d{4,}/.test(id)) {
      return {method: 'attribute_equals', attribute: 'id', value: id};
    }
    for (const name of ['data-testid', 'data-id', 'name', 'aria-label', 'role']) {
      const value = element.getAttribute(name);
      if (value) return {method: 'attribute_equals', attribute: name, value};
    }
    return {method: 'tag', attribute: '', value: element.tagName.toLowerCase()};
  };
  const targetMatch = (element) => {
    const value = targetValue(element);
    const id = element.getAttribute('id');
    if (id === value) return {method: 'attribute_equals', attribute: 'id', value};
    for (const name of [
      'data-target-selection-name', 'data-testid', 'data-id', 'name',
      'aria-label', 'placeholder', 'type', 'role'
    ]) {
      if (element.getAttribute(name) === value) {
        return {method: 'attribute_equals', attribute: name, value};
      }
    }
    return {method: value ? 'text_contains' : 'tag', attribute: '', value};
  };
  const anchorXPath = (anchors) => {
    if (!anchors.length) return '';
    const scopes = anchors.map(pathScope);
    const groups = [];
    anchors.forEach((anchor, index) => {
      const scope = scopes[index];
      const previous = groups.at(-1);
      if (previous?.scope === scope) previous.anchors.push(anchor);
      else groups.push({scope, anchors: [anchor]});
    });
    let xpath = `//${pathGroupFragment(groups[0].scope, groups[0].anchors)}`;
    for (let index = 1; index < groups.length; index += 1) {
      const previous = groups[index - 1].scope;
      const current = groups[index].scope;
      const fragment = pathGroupFragment(current, groups[index].anchors);
      if (!previous.contains(current)) return '';
      xpath += `//${fragment}`;
    }
    return xpath;
  };
  const scopeXPath = (anchors, target) => {
    if (!anchors.length) return xpathCandidate(target);
    const scopes = anchors.map(pathScope);
    const xpath = anchorXPath(anchors);
    if (!xpath) return siblingScopeXPath(anchors, target);
    const previous = scopes[scopes.length - 1];
    return previous.contains(target)
      ? `${xpath}//${targetFragment(target)}` : siblingScopeXPath(anchors, target);
  };
  const rowIndex = (row) => [...row.parentElement.children]
    .filter(element => element.tagName === 'TR').indexOf(row);
  const rowCount = (row) => [...row.parentElement.children]
    .filter(element => element.tagName === 'TR').length;
  const tableXPathWithinRange = (rangeAnchors, table) => {
    if (!rangeAnchors.length) return xpathCandidate(table, false);
    const rangeElement = pathScope(rangeAnchors.at(-1));
    const rangeXPath = anchorXPath(rangeAnchors);
    const tables = [
      ...(rangeElement.tagName === 'TABLE' ? [rangeElement] : []),
      ...rangeElement.querySelectorAll('table')
    ];
    const index = tables.indexOf(table);
    if (!rangeXPath || index < 0) return '';
    return rangeElement === table
      ? rangeXPath : `(${rangeXPath}//table)[${index + 1}]`;
  };
  const rowMapping = (anchors, target) => {
    const targetRow = target.closest('tr');
    const targetTable = targetRow?.closest('table');
    if (!anchors.length || !targetRow || !targetTable) {
      return null;
    }
    // 最後の F1 から外側へ候補行を調べ、末尾の F1 条件を最も多く包含し、
    // 対象表と同じ行 index を持つ tr を基準行にする。内嵌 table の兄弟セルも
    // 1つの外側業務行として AND 検索できる。
    const sourceRows = [];
    let candidateRow = anchors.at(-1).closest('tr');
    while (candidateRow) {
      const candidateTable = candidateRow.closest('table');
      let sourceStart = anchors.length - 1;
      while (sourceStart > 0 && candidateRow.contains(anchors[sourceStart - 1])) {
        sourceStart -= 1;
      }
      if (
        candidateTable && candidateTable !== targetTable
        && rowIndex(candidateRow) >= 0
        && (rowCount(targetRow) === 1 || rowIndex(candidateRow) === rowIndex(targetRow))
      ) {
        sourceRows.push({row: candidateRow, sourceStart});
      }
      candidateRow = candidateRow.parentElement?.closest('tr');
    }
    sourceRows.sort((left, right) => left.sourceStart - right.sourceStart);
    const sourceChoice = sourceRows[0];
    if (!sourceChoice) return null;
    const sourceRow = sourceChoice.row;
    const sourceTable = sourceRow.closest('table');
    // 内側 table 同士が同じ外側 tr にある場合は行番号マッピングではなく、
    // 共通行に兄弟条件を AND 結合した通常パスとして扱う。
    const sharedOuterRow = sourceRow.parentElement?.closest('tr');
    if (sharedOuterRow?.contains(target)) return null;
    // 外側 table の行が対象 table 全体を内包する場合、その F1 は基準行ではなく
    // 共通範囲である。偶然同じ index でも跨表対応行として扱わない。
    if (sourceRow.contains(targetTable) || targetRow.contains(sourceTable)) return null;
    // 選択時に対応行であることを確認する。保存するのは行番号ではなく、
    // 実行時に基準行から番号を再計算するための検索情報だけとする。
    if (
      rowIndex(sourceRow) < 0
      || (rowCount(targetRow) !== 1 && rowIndex(sourceRow) !== rowIndex(targetRow))
    ) return null;
    // 同じ基準行で選んだ複数の F1 は AND 条件としてまとめる。
    // これにより、共通の外側要素を選べない画面でも複数列で行を一意にできる。
    const sourceStart = sourceChoice.sourceStart;
    const sourceAnchors = anchors.slice(sourceStart);
    const rangeAnchors = anchors.slice(0, sourceStart);
    if (rangeAnchors.some(anchor => !pathScope(anchor).contains(targetTable))) return null;
    const sourceXPath = rangeAnchors.length
      ? anchorXPath(anchors) : commonAnchorXPath(sourceAnchors, sourceRow);
    const targetTableXPath = tableXPathWithinRange(rangeAnchors, targetTable);
    if (!sourceXPath || xpathCount(sourceXPath) !== 1 ||
        !targetTableXPath || xpathCount(targetTableXPath) !== 1) return null;
    return {
      operation: 'same_row_index',
      target_row_mode: rowCount(targetRow) === 1 ? 'only_row' : 'same_index',
      source_step_count: sourceAnchors.length,
      source: {selector_type: 'xpath', selector: sourceXPath},
      target_table: {selector_type: 'xpath', selector: targetTableXPath},
      row_selector: ':scope > tbody > tr, :scope > tr',
      target: {selector_type: 'xpath', selector: `.//${targetFragment(target)}`}
    };
  };
  const updatePathBanner = () => {
    const lines = pathAnchors.map(
      (element, index) => `${index + 1}. ${displayName(element)}`
    );
    banner.textContent = [
      activeText,
      `(${pathAnchors.length})`,
      ...lines,
    ].join('\n');
  };
  const enterSelectionMode = (keyName) => {
    selecting = true;
    selectionKey = keyName;
    if (hovered?.isConnected) {
      hovered.classList.add('__sf-flow-hover');
      updateFeedback();
    }
    updatePathBanner();
  };
  const leaveSelectionMode = () => {
    selecting = false;
    selectionKey = '';
    if (hovered) hovered.classList.remove('__sf-flow-hover');
    highlight.hidden = true;
    elementInfo.hidden = true;
    banner.textContent = __WAITING_TEXT__;
  };
  const finish = (element) => {
    const result = elementResult(element);
    if (pathAnchors.length) {
      const mapping = rowMapping(pathAnchors, element);
      const resolvedXPath = mapping ? '' : scopeXPath(pathAnchors, element);
      result.path_steps = [
        ...pathAnchors.map(anchor => ({
          ...elementResult(anchor), path_match: scopeMatch(anchor)
        })),
        {...result, path_match: targetMatch(element)}
      ];
      result.path_xpath = resolvedXPath && xpathCount(resolvedXPath) === 1
        ? resolvedXPath : '';
      result.row_mapping = mapping;
    }
    window.__sfFlowPicked = result;
    clean();
  };
  const key = (event) => {
    if (event.key === 'Escape' && selecting) {
      event.preventDefault();
      event.stopPropagation();
      leaveSelectionMode();
    } else if (event.key === 'F1') {
      event.preventDefault();
      event.stopPropagation();
      enterSelectionMode('F1');
    } else if (event.key === 'F2') {
      event.preventDefault();
      event.stopPropagation();
      enterSelectionMode('F2');
    } else if (event.key === 'Backspace' && selecting && pathAnchors.length) {
      event.preventDefault();
      event.stopPropagation();
      pathAnchors.pop().classList.remove('__sf-flow-path-anchor');
      updatePathBanner();
    } else if (event.key === 'Enter' && selecting && hovered) {
      event.preventDefault();
      event.stopPropagation();
      finish(hovered);
    }
  };
  // 画面を開いた直後からホバー要素を追跡し、F1/F2 を一回押すだけで選択する。
  // click は奪わないため、対象画面内の移動や展開操作は選択中も継続できる。
  banner.textContent = __WAITING_TEXT__;
  document.addEventListener('mouseover', over, true);
  document.addEventListener('click', choose, true);
  document.addEventListener('keydown', key, true);
  document.addEventListener('scroll', updateFeedback, true);
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
