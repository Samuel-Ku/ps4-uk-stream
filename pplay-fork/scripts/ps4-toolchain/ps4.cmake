set(CMAKE_SYSTEM_NAME Orbis)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

set(CMAKE_C_COMPILER clang)
set(CMAKE_CXX_COMPILER clang++)
set(CMAKE_LINKER ld.lld)

set(CMAKE_FIND_ROOT_PATH ${OPENORBIS})

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

set(CMAKE_C_FLAGS "--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot ${OPENORBIS} -isystem ${OPENORBIS}/include")
set(CMAKE_CXX_FLAGS "--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot ${OPENORBIS} -isystem ${OPENORBIS}/include -isystem ${OPENORBIS}/include/c++/v1")
set(CMAKE_EXE_LINKER_FLAGS "-fuse-ld=lld -pie -Wl,--script,${OPENORBIS}/link.x -Wl,--eh-frame-hdr -L${OPENORBIS}/lib -lc -lkernel -lc++ ${OPENORBIS}/lib/crt1.o")
set(CMAKE_SHARED_LINKER_FLAGS "-fuse-ld=lld -pie -Wl,--script,${OPENORBIS}/link.x -Wl,--eh-frame-hdr -L${OPENORBIS}/lib -lc -lkernel -lc++")
