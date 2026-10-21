"use client";

import { useEffect, useId, useRef } from "react";
import { X } from "lucide-react";

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
  className = "",
  trapFocus = false,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
  trapFocus?: boolean;
}) {
  const titleId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  useEffect(() => { close.current = onClose; }, [onClose]);
  useEffect(() => {
    if (!open || !trapFocus) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = dialog.current;
    const focusables = () => Array.from(panel?.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
    ) ?? []).filter((el) => el.getClientRects().length > 0);
    (panel?.querySelector<HTMLElement>('input:not(:disabled), select:not(:disabled), button:not(.modal-close):not(:disabled)') ?? panel)?.focus();
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (event.key !== "Tab") return;
      const elements = focusables();
      if (!elements.length) { event.preventDefault(); panel?.focus(); return; }
      const first = elements[0], last = elements[elements.length - 1];
      if (!panel?.contains(document.activeElement)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus(); return;
      }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => { document.removeEventListener("keydown", handleKey); if (previous?.isConnected) previous.focus(); };
  }, [open, trapFocus]);

  if (!open) return null;

  return (
    <div className="modal-overlay open" onClick={onClose}>
      <div ref={dialog} className={`modal ${className}`} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
        <div className="modal-hdr">
          <div id={titleId} className="modal-title">{title}</div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
