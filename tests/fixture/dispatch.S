/* FS/GS functions are analysis-only controls: the driver never calls them. */
.text
.macro DISPATCH name, mode
.p2align 4
.globl \name
.type \name, @function
\name:
  cmpl $2, %edi
  ja .Ldefault\@
  movl %edi, %eax
.if \mode == 1
  notrack jmp *.Ltable\@(,%rax,8)
.elseif \mode == 2
  jmp *%fs:.Ltable\@(,%rax,8)
.elseif \mode == 3
  jmp *%gs:.Ltable\@(,%rax,8)
.else
  jmp *.Ltable\@(,%rax,8)
.endif
.Lcase0\@:
  movl $11, %eax
  ret
.Lcase1\@:
  movl $22, %eax
  ret
.Lcase2\@:
  movl $33, %eax
  ret
.Ldefault\@:
  movl $-1, %eax
  ret
.size \name, .-\name
.pushsection .rodata
.p2align 3
.Ltable\@:
  .quad .Lcase0\@, .Lcase1\@, .Lcase2\@
.popsection
.endm

DISPATCH dispatch_plain, 0
DISPATCH dispatch_notrack, 1
DISPATCH dispatch_fs, 2
DISPATCH dispatch_gs, 3

.section .note.GNU-stack,"",@progbits
