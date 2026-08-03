// PS4 stubs for the libcross2d cross-platform gl2/* symbols.
//
// The OpenOrbisSDK doesn't ship a static libGLESv2 (only the dynamic
// SceGnm .so files for the PS4 firmware), so the upstream libcross2d
// gl2/gl_renderer.cpp / gl_texture.cpp / gl_texture_buffer.cpp —
// which call glClearColor / glGenBuffers / glDisableVertexAttribArray
// / etc. directly — can't link on PS4. The PS4 platform code in
// source/platforms/ps4/ps4_render.cpp uses Sony's GNM API directly
// and doesn't need the GLES2 symbols.
//
// libcross2d/CMakeLists.txt excludes gl2/*.c* from the build for PS4,
// but other files that ARE compiled still reference those symbols
// transitively:
//
//   - VertexArray.cpp (source/skeleton/sfml/) calls glGenBuffers,
//     glBindBuffer, glBufferData, glDeleteBuffers directly.
//   - Font.cpp uses Texture (typedef'd to GLTexture) and constructs
//     `GLTexture(const Vector2f&, Format)`.
//   - VideoTexture.cpp (in pplay) inherits from GLTextureBuffer.
//
// We provide no-op implementations of all those symbols here so the
// link succeeds. The PS4 build's renderer path is GNM-only; these
// classes never get a real GL context to draw to on PS4, so doing
// nothing is safe. (The GUI never appears — the streaming playback
// path is what's exercised in the goldhen PKG, see
// docs/superpowers/plans/2026-08-01-ps4-uk-stream-impl.md.)
//
// __PSP4__ is the macro libcross2d/CMakeLists.txt defines for its PS4
// platform sources; pplay itself uses __PS4__ (pplay/CMakeLists.txt)
// which doesn't reach this translation unit.

#ifdef __PSP4__

// The GLES2 function signatures use GLenum / GLsizei / GLuint / etc.,
// and gl_renderer.h references GLenum in a member declaration. SDL's
// opengles2 header is what c2d_gl2.h pulls in on PS4 (see the
// __SDL2__ + __GLES2__ + !__GLAD__ branch in c2d_gl2.h), so include
// it first so the GL types are visible before the gl_renderer.h
// header is parsed.
#include <SDL2/SDL_opengles2.h>
// GL_QUADS is a desktop GL constant that GLES2 removed; c2d_gl2.h
// polyfills it (see "#ifndef GL_QUADS / #define GL_QUADS 0x0006" in
// c2d_gl2.h) when included via c2d.h. We're including the gl2/
// headers directly here, so do the same polyfill ourselves.
#ifndef GL_QUADS
#define GL_QUADS 0x0006
#endif

#include "cross2d/platforms/gl2/gl_renderer.h"
#include "cross2d/platforms/gl2/gl_texture.h"
#include "cross2d/platforms/gl2/gl_texture_buffer.h"

namespace c2d {
    // GLRenderer — base for SDL2Renderer (which IS compiled for PS4).
    GLRenderer::GLRenderer(const Vector2f &size) : Renderer(size) {}
    GLRenderer::~GLRenderer() = default;
    void GLRenderer::initGL() {}
    void GLRenderer::draw(VertexArray *, const Transform &, Texture *) {}
    void GLRenderer::clear() {}
    void GLRenderer::flip(bool, bool) {}

    void CheckOpenGLError(const char *, const char *, int) {}

    // GLTexture — used by Font.cpp via `#define C2DTexture GLTexture`.
    GLTexture::GLTexture(const std::string &) : Texture() {}
    GLTexture::GLTexture(const unsigned char *, int) : Texture() {}
    GLTexture::GLTexture(const Vector2f &size, Format format) : Texture(size, format) {}
    GLTexture::~GLTexture() = default;
    int GLTexture::save(const std::string &) { return 0; }
    int GLTexture::lock(FloatRect *, void **, int *) { return 0; }
    void GLTexture::unlock(void *) {}
    int GLTexture::resize(const Vector2i &, bool) { return 0; }
    void GLTexture::setFilter(Filter) {}

    // GLTextureBuffer — base for pplay's VideoTexture.
    GLTextureBuffer::GLTextureBuffer(const Vector2f &size, Format format)
        : Texture(size, format) {}
    GLTextureBuffer::~GLTextureBuffer() = default;
    int GLTextureBuffer::resize(const Vector2i &, bool) { return 0; }
    void GLTextureBuffer::setFilter(Filter) {}
    int GLTextureBuffer::createTexture(const Vector2f &, Format) { return 0; }
    void GLTextureBuffer::deleteTexture() {}
}

// VertexArray.cpp calls glGenBuffers / glBindBuffer / glBufferData /
// glDeleteBuffers directly. SDL2's headers (included transitively via
// c2d.h → c2d_gl2.h) declare these as imports; on PS4 there is no
// libGLESv2.a to link against, so we provide weak no-op definitions
// here. Linking against these is safe because VertexArray is never
// asked to render to a real GL context on PS4 (the GNM renderer path
// is used instead).
extern "C" {
    void glGenBuffers(GLsizei, GLuint *) {}
    void glDeleteBuffers(GLsizei, const GLuint *) {}
    void glBindBuffer(GLenum, GLuint) {}
    void glBufferData(GLenum, GLsizeiptr, const void *, GLenum) {}
    void glBufferSubData(GLenum, GLintptr, GLsizeiptr, const void *) {}
    void glEnableVertexAttribArray(GLuint) {}
    void glDisableVertexAttribArray(GLuint) {}
    void glVertexAttribPointer(GLuint, GLint, GLenum, GLboolean, GLsizei, const void *) {}
    GLenum glGetError() { return 0; }
}

#endif // __PSP4__