import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/demo', name: 'OfflineDemo', component: () => import('../views/Demo.vue') },
  { path: '/live', name: 'LiveHome', component: () => import('../views/Home.vue') },
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/Demo.vue'),
  },
  {
    path: '/task/:id',
    name: 'TaskMonitor',
    component: () => import('../views/TaskMonitor.vue'),
  },
  {
    path: '/task/:id/clarify',
    name: 'Clarifications',
    component: () => import('../views/Clarifications.vue'),
  },
  {
    path: '/task/:id/approve',
    name: 'Approvals',
    component: () => import('../views/Approvals.vue'),
  },
  {
    path: '/task/:id/approval/:approvalId',
    name: 'ApprovalDetail',
    component: () => import('../views/ApprovalDetail.vue'),
  },
  {
    // DesignPlan.vue is a full page — "打开设计页面" must land on the
    // design, not on the approval page (the T-03 redirect sent users to
    // 方案审批 by mistake).
    path: '/task/:id/design',
    name: 'DesignPlan',
    component: () => import('../views/DesignPlan.vue'),
  },
  {
    path: '/task/:id/report',
    name: 'Report',
    component: () => import('../views/Report.vue'),
  },
  {
    path: '/task/:id/assets',
    name: 'AssetsMap',
    component: () => import('../views/AssetsMap.vue'),
  },
  {
    path: '/tasks',
    name: 'TasksOverview',
    component: () => import('../views/TasksOverview.vue'),
  },
  {
    path: '/catalog',
    name: 'DataCatalog',
    component: () => import('../views/DataCatalog.vue'),
  },
  {
    path: '/metrics',
    name: 'MetricLibrary',
    component: () => import('../views/MetricLibrary.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
