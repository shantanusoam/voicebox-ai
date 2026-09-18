file(GLOB SBC_SRCS ${CMAKE_CURRENT_LIST_DIR}/src/*.c)
idf_component_register(SRCS ${SBC_SRCS}
                       INCLUDE_DIRS include)
