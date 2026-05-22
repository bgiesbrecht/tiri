import * as React from "react";
import {
  Toast,
  ToastClose,
  ToastDescription,
  ToastProvider as RadixToastProvider,
  ToastTitle,
  ToastViewport,
} from "@/components/ui/toast";

interface ToastInput {
  title: string;
  description?: string;
  variant?: "default" | "success" | "destructive";
  durationMs?: number;
}

interface ToastEntry extends ToastInput {
  id: number;
}

interface ToastContextValue {
  toast: (input: ToastInput) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

/**
 * Wrap the app in <ToastProvider> at the top level. Components call
 * `useToast()` to get `toast({title, description, variant})`. Each toast
 * auto-dismisses after `durationMs` (default 4s) but can be swiped or
 * clicked away via the close button.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<ToastEntry[]>([]);
  const nextId = React.useRef(1);

  const toast = React.useCallback((input: ToastInput) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { id, ...input }]);
  }, []);

  const remove = React.useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ toast }}>
      <RadixToastProvider duration={4000}>
        {children}
        {toasts.map((t) => (
          <Toast
            key={t.id}
            variant={t.variant || "default"}
            duration={t.durationMs || 4000}
            onOpenChange={(open) => {
              if (!open) remove(t.id);
            }}
          >
            <div className="grid gap-0.5">
              <ToastTitle>{t.title}</ToastTitle>
              {t.description ? (
                <ToastDescription>{t.description}</ToastDescription>
              ) : null}
            </div>
            <ToastClose />
          </Toast>
        ))}
        <ToastViewport />
      </RadixToastProvider>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used inside <ToastProvider>");
  }
  return ctx;
}
