#include <ATen/cpu/Utils.h>
#include <torch/csrc/cpu/Module.h>
#include <torch/csrc/utils/pybind.h>

namespace torch::cpu {

void initModule(PyObject* module) {
  auto m = py::handle(module).cast<py::module>();

  auto cpu = m.def_submodule("_cpu", "cpu related pybind.");
  cpu.def("_is_cpu_support_avx2", at::cpu::is_cpu_support_avx2);
  cpu.def("_is_cpu_support_avx512", at::cpu::is_cpu_support_avx512);
  cpu.def("_is_cpu_support_vnni", at::cpu::is_cpu_support_vnni);
}

} // namespace torch::cpu
