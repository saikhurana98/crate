// Tiny reactive core — the "React-ish" bits without a framework.
//
// - render is a pure view(): State -> HTML string
// - createRenderer batches renders (one per animation frame) and RECONCILES the
//   live DOM against the new HTML (morph) instead of replacing innerHTML, so the
//   body is never wiped: focus, scroll, input values, drag state and CSS
//   transitions on unchanged nodes are preserved.
// - effect() runs a side effect once at mount (SSE, intervals).

export function effect(fn: () => void): void {
  fn();
}

export function createRenderer(root: HTMLElement, view: () => string): () => void {
  let queued = false;
  const flush = (): void => {
    queued = false;
    const next = document.createElement("body");
    next.innerHTML = view();
    morphChildren(root, next);
  };
  return () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(flush);
  };
}

// ---- morphdom-lite: patch `oldEl`'s children to match `newEl`'s, positionally ----
function morphChildren(oldEl: Element, newEl: Element): void {
  let oldChild = oldEl.firstChild;
  let newChild = newEl.firstChild;
  while (newChild) {
    const newNext = newChild.nextSibling;
    if (!oldChild) {
      oldEl.appendChild(newChild); // adopt (detaches from newEl)
      newChild = newNext;
      continue;
    }
    const oldNext = oldChild.nextSibling;
    morphNode(oldChild, newChild);
    oldChild = oldNext;
    newChild = newNext;
  }
  while (oldChild) {
    const next = oldChild.nextSibling;
    oldEl.removeChild(oldChild);
    oldChild = next;
  }
}

function morphNode(oldNode: ChildNode, newNode: ChildNode): void {
  if (oldNode.nodeType !== newNode.nodeType || oldNode.nodeName !== newNode.nodeName) {
    oldNode.parentNode!.replaceChild(newNode, oldNode);
    return;
  }
  if (oldNode.nodeType === Node.TEXT_NODE) {
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue;
    return;
  }
  if (oldNode.nodeType === Node.ELEMENT_NODE) {
    morphAttrs(oldNode as Element, newNode as Element);
    morphChildren(oldNode as Element, newNode as Element);
  }
}

function morphAttrs(oldEl: Element, newEl: Element): void {
  for (const attr of Array.from(newEl.attributes)) {
    if (oldEl.getAttribute(attr.name) !== attr.value) oldEl.setAttribute(attr.name, attr.value);
  }
  for (const attr of Array.from(oldEl.attributes)) {
    if (!newEl.hasAttribute(attr.name)) oldEl.removeAttribute(attr.name);
  }
  // We only ever sync attributes, never the live `.value` property — so a field
  // the user is typing into is never clobbered by a background re-render.
}
