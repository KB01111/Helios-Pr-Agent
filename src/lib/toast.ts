import { toast } from "sonner";

export const toastOk = (title: string, body?: string) => toast.success(title, { description: body });
export const toastErr = (title: string, body?: string) => toast.error(title, { description: body });
