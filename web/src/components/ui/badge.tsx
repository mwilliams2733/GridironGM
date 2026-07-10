import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium tracking-wide",
  {
    variants: {
      variant: {
        default: "border-field-600 bg-field-800 text-field-200",
        hash: "border-hash-600/40 bg-hash-500/10 text-hash-500",
        amber: "border-amber-600/40 bg-amber-500/10 text-amber-500",
        crimson: "border-crimson-600/40 bg-crimson-500/10 text-crimson-500",
        sky: "border-sky-500/40 bg-sky-500/10 text-sky-500",
        outline: "border-field-600 bg-transparent text-field-300",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
